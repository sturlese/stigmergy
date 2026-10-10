import datetime as dt
import json
import re
import subprocess
from dataclasses import dataclass

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from stigmergy.admin import routes
from stigmergy.admin.routes import MAX_ADMIN_BODY_BYTES, compose
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.service import AdminRefused, AdminService
from stigmergy.capture import evidence, queue, schema, uploads
from stigmergy.capture.errors import FetchRejected, FetchUnavailable
from stigmergy.capture.fetch import FetchedArtifact
from stigmergy.changes import store as change_store
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.contract import expected_librarian_skill
from stigmergy.knowledge.contradictions import Contradiction
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import ContradictionClaim, FilingPlan
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker
from stigmergy.server import roster
from stigmergy.server.identity import hash_token
from stigmergy.server.settings import Settings
from tests import testdb
from tests.roster import seed_roster

TOKEN = "test-admin-token"
CSRF = {"X-Stigmergy-CSRF": "1", "Origin": "http://testserver"}
MASTER = "morgan"
MEMBER = "ana"
ROSTER = {
    MASTER: {"display_name": "Morgan", "groups": ["brain-admins", "finance"], "default_audience": ["public"]},
    MEMBER: {"display_name": "Ana", "groups": ["finance"], "default_audience": ["finance"]},
}
ACME_ENTITY = "wiki/entities/Acme.md"
ENTITY_ABSENCE = {
    "found": False,
    "entity": None,
    "knowledge": [],
    "knowledge_note": "No visible entity was found.",
    "knowledge_state": "not_found",
    "knowledge_truncated": False,
    "knowledge_returned": 0,
    "knowledge_cap": 20,
    "sources": [],
    "sources_truncated": False,
    "sources_returned": 0,
    "sources_cap": 40,
}


@dataclass
class AdminRig:
    client: TestClient
    conn: object
    repo: object
    evidence: evidence.MemoryEvidenceStore
    cookie: str

    @property
    def auth(self):
        return {"Cookie": self.cookie, **CSRF}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def admin_rig(tmp_path, monkeypatch):
    monkeypatch.delenv("STIGMERGY_PUBLIC_HOST", raising=False)
    repo = tmp_path / "brain"
    repo.mkdir()
    (repo / "wiki" / "notes").mkdir(parents=True)
    skill = repo / ".claude" / "skills" / "librarian" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(expected_librarian_skill())
    (repo / "wiki" / "notes" / "Welcome.md").write_text(
        render_page(
            path="wiki/notes/Welcome.md",
            role="note",
            title="Welcome",
            body="# Welcome\n\nInitial team knowledge about [[Acme]].",
            acl=None,
            created=dt.date(2026, 8, 1),
            updated=dt.date(2026, 8, 1),
        )
    )
    (repo / "wiki" / "entities").mkdir()
    (repo / "wiki" / "entities" / "Acme.md").write_text(
        render_page(
            path=ACME_ENTITY,
            role="entity",
            title="Acme",
            body="# Acme\n\nAcme is the customer the welcome note introduces.",
            acl=None,
            entity_type="organization",
            aliases=("Acme Corp",),
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
        cursor.execute("DELETE FROM layer_erasures")
        cursor.execute(
            "UPDATE worker_heartbeat SET heartbeat_at = now(), state = 'starting' WHERE singleton"
        )
    build.rebuild(conn, str(repo), build_embedder("fake"))
    seed_roster(conn, ROSTER, admins={MASTER})
    with conn.cursor() as cursor:
        cursor.execute("UPDATE roster_tokens SET sha256 = %s WHERE subject = %s",
                       (hash_token(TOKEN), MASTER))

    server_settings = Settings(
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
        evidence=evidence_store,
    )
    with TestClient(app) as client:
        signed_in = client.post("/admin/api/session", json={"token": TOKEN}, headers=CSRF)
        cookie = signed_in.headers["set-cookie"].split(";", 1)[0]
        yield AdminRig(client, conn, repo, evidence_store, cookie)
    conn.close()


def test_the_bare_host_serves_the_public_landing_and_nothing_is_indexed(admin_rig, monkeypatch):
    monkeypatch.setenv(routes.LANDING_ENV, "on")
    monkeypatch.setattr(routes, "STATIC_DIR", admin_rig.repo.parent / "static")
    (admin_rig.repo.parent / "static").mkdir()
    for page in ("landing.html", "index.html"):
        (admin_rig.repo.parent / "static" / page).write_text(
            '<meta name="csp-nonce" content="__CSP_NONCE__"><title>Stigmergy</title>')

    landing = admin_rig.client.get("/", follow_redirects=False)

    assert landing.status_code == 200 and "<title>Stigmergy</title>" in landing.text
    assert "__CSP_NONCE__" not in landing.text and "nonce-" in landing.headers["content-security-policy"]
    assert landing.headers["cache-control"] == "no-cache"
    assert admin_rig.client.head("/").status_code == 200
    assert admin_rig.client.post("/", json={}).status_code == 405      # reaches the inner app
    for response in (landing, admin_rig.client.get("/mcp"), admin_rig.client.get("/admin/api/meta"),
                     admin_rig.client.get("/admin/")):
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
        assert response.headers.get_list("strict-transport-security") == [
            "max-age=31536000; includeSubDomains"]
        assert response.headers.get_list("x-content-type-options") == ["nosniff"]



def test_the_operators_legal_pages_are_served_by_the_landing(monkeypatch, tmp_path):
    (tmp_path / "landing.html").write_text("<title>Stigmergy</title>")
    legal = tmp_path / "legal"
    legal.mkdir()
    (legal / "sub-processors.md").write_text("# Sub-processors\n\n<script>x</script>\n")
    monkeypatch.setattr(routes, "STATIC_DIR", tmp_path)
    monkeypatch.setenv(routes.LEGAL_DIR_ENV, str(legal))
    monkeypatch.setenv(routes.LANDING_ENV, "on")
    client = TestClient(routes._Branch(Starlette(), Starlette()))

    assert "<title>Stigmergy</title>" in client.get("/sub-processors").text
    body = client.get("/legal/sub-processors")
    assert body.status_code == 200
    html = body.json()["html"]
    assert html.startswith("<h1>") and "<script" not in html
    assert client.get("/legal/privacy").status_code == 404


def test_a_deployment_without_legal_pages_serves_none(monkeypatch, tmp_path):
    monkeypatch.delenv(routes.LEGAL_DIR_ENV, raising=False)
    client = TestClient(routes._Branch(Starlette(), Starlette()))

    assert client.get("/legal/sub-processors").status_code == 404

def test_admin_is_master_only_and_serves_a_secured_login_shell(admin_rig):
    unauthorized = admin_rig.client.get("/admin/api/meta")
    assert unauthorized.status_code == 401

    meta = admin_rig.client.get("/admin/api/meta", headers=admin_rig.auth)
    assert meta.status_code == 200
    assert meta.json()["actor"] == {"subject": MASTER, "display_name": "Morgan"}
    assert meta.json()["statuses"] == ["queued", "processing", "landed", "failed"]
    assert "no-store" in meta.headers["cache-control"]
    assert "default-src 'none'" in meta.headers["content-security-policy"]

    shell = admin_rig.client.get("/admin/")
    assert shell.status_code == 200
    assert "<title>Stigmergy</title>" in shell.text
    assert '<script type="module"' in shell.text
    assert "no-cache" in shell.headers["cache-control"]


def test_the_shell_hands_its_style_nonce_only_to_itself(admin_rig):
    first = admin_rig.client.get("/admin/")
    second = admin_rig.client.get("/admin/")
    api = admin_rig.client.get("/admin/api/me", headers=admin_rig.auth)

    nonces = [re.search(r'name="csp-nonce" content="([^"]+)"', r.text).group(1) for r in (first, second)]
    assert nonces[0] != nonces[1] and "__CSP_NONCE__" not in first.text
    for response, nonce in zip((first, second), nonces, strict=True):
        policy = response.headers["content-security-policy"]
        assert f"style-src 'self' 'nonce-{nonce}'" in policy
        assert "script-src 'self';" in policy and "unsafe" not in policy
        assert response.headers.get_list("content-security-policy") == [policy]
    assert "nonce" not in api.headers["content-security-policy"]


def test_admin_entity_dossier_uses_the_shared_bounded_descriptor(admin_rig):
    unauthenticated = admin_rig.client.get("/admin/api/entities/unknown/dossier")
    response = admin_rig.client.get(
        "/admin/api/entities/unknown/dossier", headers=admin_rig.auth
    )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json() == ENTITY_ABSENCE


def test_admin_lists_entity_pages_and_serves_a_dossier_by_title_alias_or_path(admin_rig):
    listing = admin_rig.client.get("/admin/api/entities", headers=admin_rig.auth)
    by_title = admin_rig.client.get("/admin/api/entities/Acme/dossier", headers=admin_rig.auth)
    by_alias = admin_rig.client.get(
        "/admin/api/entities/acme%20corp/dossier", headers=admin_rig.auth
    )

    assert listing.status_code == 200
    assert listing.json() == {
        "count": 1,
        "entities": [
            {"path": ACME_ENTITY, "title": "Acme", "entity_type": "organization",
             "aliases": ["Acme Corp"], "layers": ["public"]}
        ],
    }
    assert by_title.status_code == by_alias.status_code == 200
    assert by_title.json() == by_alias.json()
    dossier = by_title.json()
    assert dossier["found"] is True
    assert dossier["entity"]["path"] == ACME_ENTITY
    assert dossier["entity"]["aliases"] == ["Acme Corp"]
    assert dossier["entity"]["excerpt"].startswith("Acme is the customer")
    assert [item["path"] for item in dossier["knowledge"]] == ["wiki/notes/Welcome.md"]
    # the exact page path is a key too, through the same service the routes call
    service = AdminService(
        admin_rig.conn,
        server_settings=Settings(
            knowledge_repo=str(admin_rig.repo),
            dsn=testdb.dsn(),
            embedder="fake",
            llm="fake",
        ),
        principal=roster.principal(admin_rig.conn, MASTER),
        evidence=admin_rig.evidence,
    )
    assert service.entity_dossier(ACME_ENTITY) == dossier



def test_admin_entity_reads_are_audited_like_every_other_read(admin_rig):
    from stigmergy.server.audit import ensure_audit_table

    ensure_audit_table(admin_rig.conn)
    admin_rig.client.get("/admin/api/entities", headers=admin_rig.auth)
    admin_rig.client.get("/admin/api/entities/Acme/dossier", headers=admin_rig.auth)
    with admin_rig.conn.cursor() as cur:
        cur.execute("SELECT identity, tool FROM audit_log ORDER BY id")
        rows = cur.fetchall()
    assert (MASTER, "entity_vocabulary") in rows and (MASTER, "describe_entity") in rows

def test_admin_service_refuses_a_principal_that_is_not_an_administrator(admin_rig):
    server_settings = Settings(
        knowledge_repo=str(admin_rig.repo),
        knowledge_branch="main",
        dsn=testdb.dsn(),
        embedder="fake",
        llm="fake",
    )

    with pytest.raises(AdminRefused, match="not an administrator"):
        AdminService(
            admin_rig.conn,
            server_settings=server_settings,
            principal=roster.principal(admin_rig.conn, MEMBER),
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
        dsn=testdb.dsn(),
        embedder="fake",
        llm="fake",
    )
    app = compose(
        Starlette(),
        conn=admin_rig.conn,
        server_settings=server_settings,
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
    path = admin_rig.repo / "layers" / "finance" / "wiki" / "notes" / "Renewal.md"
    path.parent.mkdir(parents=True, exist_ok=True)
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
        source = admin_rig.repo.joinpath("layers", "finance", *claim.source.split("/"))
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
    assert [item["path"] for item in listing.json()["contradictions"][0]["paths"]] == [
        "layers/finance/wiki/notes/Renewal.md"]
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
    deletion = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={"action": "delete", "entities": [ACME_ENTITY], "rationale": "Duplicate identity"},
    )
    garden = admin_rig.client.post(
        "/admin/api/gardener/trigger",
        headers=admin_rig.auth,
        json={"rationale": "Verify corpus health"},
    )
    recompile = admin_rig.client.post(
        "/admin/api/knowledge/recompile",
        headers=admin_rig.auth,
        json={"rationale": "Rebuild derived knowledge from immutable sources"},
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

    assert deletion.status_code == garden.status_code == recompile.status_code == retried.status_code == 200
    assert queue.get_submission_trace(admin_rig.conn, deletion.json()["id"])["operation"] == schema.ENTITY
    assert queue.get_submission_trace(admin_rig.conn, garden.json()["id"])["operation"] == schema.GARDEN
    assert queue.get_submission_trace(admin_rig.conn, recompile.json()["id"])["request"]["mode"] == "recompile"
    assert retried.json()["status"] == schema.QUEUED


def test_entity_merge_route_requires_typed_verifiable_evidence(admin_rig):
    entities = [ACME_ENTITY, "wiki/entities/Acme Holdings.md"]
    assertion = {
        "path": "sources/2026/08/00000000-0000-4000-8000-000000000001.md",
        "assertion": "Acme and Acme Holdings are the same organization.",
    }
    rationale_only = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={
            "action": "merge",
            "entities": entities,
            "rationale": "These look alike.",
        },
    )
    by_id = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={
            "action": "merge",
            "entities": ["ent_00000000-0000-4000-8000-000000000001", ACME_ENTITY],
            "rationale": "Opaque ids are not a key any more.",
            "evidence": {"source_assertions": [assertion]},
        },
    )
    evidenced = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={
            "action": "merge",
            "entities": entities,
            "rationale": "The source states both names refer to one organization.",
            "evidence": {"source_assertions": [assertion]},
        },
    )

    assert rationale_only.status_code == 400
    assert "verifiable evidence" in rationale_only.json()["error"]
    assert by_id.status_code == 400
    assert "entity page paths" in by_id.json()["error"]
    assert evidenced.status_code == 200
    request = queue.get_submission_trace(admin_rig.conn, evidenced.json()["id"])["request"]
    assert request["entities"] == entities
    assert request["evidence"] == {"source_assertions": [assertion]}


def test_entity_rename_route_needs_a_new_title_and_the_writer_keeps_the_old_one_as_alias(
    admin_rig,
):
    untitled = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={"action": "rename", "entities": [ACME_ENTITY], "rationale": "Rebranded."},
    )
    renamed = admin_rig.client.post(
        "/admin/api/entities/operation",
        headers=admin_rig.auth,
        json={
            "action": "rename",
            "entities": [ACME_ENTITY],
            "new_title": "Acme Holdings",
            "rationale": "The company rebranded after the acquisition.",
        },
    )

    assert untitled.status_code == 400
    assert "new title" in untitled.json()["error"]
    assert renamed.status_code == 200
    queued = queue.get_submission_trace(admin_rig.conn, renamed.json()["id"])
    assert queued["operation"] == schema.ENTITY
    assert queued["request"]["new_title"] == "Acme Holdings"

    item, outcome = worker.process_next(
        admin_rig.conn,
        WriterDeps(
            config.Settings(repo=str(admin_rig.repo), branch="main", backend="scripted"),
            admin_rig.evidence,
            ScriptedPlanner(FilingPlan(summary="unused by an entity operation")),
            str(admin_rig.repo),
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["change_id"]
    paths = _git(admin_rig.repo, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert ACME_ENTITY not in paths
    moved = parse_page(
        "wiki/entities/Acme Holdings.md",
        _git(admin_rig.repo, "show", "main:wiki/entities/Acme Holdings.md") + "\n",
    )
    assert moved.title == "Acme Holdings"
    assert moved.aliases == ("Acme Corp", "Acme")
    assert "[[Acme Holdings|Acme]]" in _git(admin_rig.repo, "show", "main:wiki/notes/Welcome.md")
    change = admin_rig.client.get(
        f"/admin/api/changes/{item['change_id']}", headers=admin_rig.auth
    ).json()["change"]
    assert change["trigger"] == "entity"
    assert set(change["path_patches"]) == {
        ACME_ENTITY, "wiki/entities/Acme Holdings.md", "wiki/notes/Welcome.md"
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


def test_index_endpoint_reports_the_filing_context_and_when_phase_4_falls_due(admin_rig):
    def capture(key: str, bytes_used: int, trimmed: int) -> None:
        submitted = admin_rig.client.post("/admin/api/captures/text", headers=admin_rig.auth, json={
            "text": f"Context pressure sample {key}.", "audience": ["public"], "idempotency_key": key})
        report = {"filing_context": {"bytes": bytes_used, "budget_bytes": 786432, "catalog": 30, "candidates": 12,
                                     "evidence": 4, "trimmed": trimmed}}
        with admin_rig.conn.cursor() as cursor:
            cursor.execute("UPDATE capture_queue SET status = 'landed', finished_at = now(), report = %s::jsonb"
                           " WHERE id = %s", (json.dumps(report), submitted.json()["id"]))

    capture("pressure-low", 98304, 0)
    body = admin_rig.client.get("/admin/api/index", headers=admin_rig.auth).json()
    assert body["filing_context"] == {"captures": 1, "max_share": 0.125, "trimmed": 0, "due": False}
    healthy = body["healthy"]

    capture("pressure-trimmed", 700000, 2)
    body = admin_rig.client.get("/admin/api/index", headers=admin_rig.auth).json()
    assert body["filing_context"]["due"] is True and body["filing_context"]["trimmed"] == 1
    assert body["healthy"] is healthy                       # a capacity signal, not an index fault


def test_graph_measures_endpoint_serves_a_scope_and_refuses_an_unknown_one(admin_rig):
    response = admin_rig.client.get("/admin/api/graph-measures", headers=admin_rig.auth)
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "brain" and body["scopes"] == ["brain", "public"]
    assert body["measures"]["pages_by_type"]["entity"] == 1
    assert body["hubs"][0]["path"] == ACME_ENTITY and body["series"]
    refused = admin_rig.client.get("/admin/api/graph-measures?scope=finance", headers=admin_rig.auth)
    assert refused.status_code == 400 and "scope must be one of brain, public" in refused.json()["error"]


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
        "/admin/api/captures?status=queued&submitter=morgan&limit=10&offset=0",
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
    # The shared writer is "starting"; this workspace sees only that nothing of its own is running.
    assert overview.json()["worker"]["heartbeat"]["state"] == "idle"
    assert listing.json()["count"] == 1
    assert listing.json()["captures"][0]["id"] == capture_id
    assert detail.json()["id"] == capture_id
    assert detail.json()["provenance"]["title"] == "Renewal approval"
    assert detail.json()["artifacts"][0]["media_type"] == schema.MEDIA_TEXT
    assert listing.json()["captures"][0]["layer_decision"] == schema.EXPLICIT
    assert detail.json()["layer_decision"] == schema.EXPLICIT
    assert detail.json()["layer_probabilities"] is None
    assert missing.status_code == 404
    assert missing.json() == {"error": "capture not found"}


def test_capture_list_filters_by_how_the_layer_was_decided(admin_rig):
    from stigmergy.capture import queue
    from stigmergy.capture.service import CaptureService

    admin_rig.client.post("/admin/api/captures/text", headers=admin_rig.auth,
                          json={"text": "Named in the backoffice.", "audience": ["finance"]})
    classified = CaptureService(admin_rig.conn, admin_rig.evidence).capture_text(
        actor=schema.Actor(subject="agent@example.com", display_name="Agent"),
        audience=("finance",),
        adapter="mcp",
        text="Chosen by the classifier.",
        idempotency_key="classified-filter-test",
        layer_decision=schema.LayerDecision(decision=schema.CLASSIFIED,
                                            probabilities={"finance": 0.9, "eng": 0.1}),
    )

    listing = admin_rig.client.get("/admin/api/captures?decision=classified", headers=admin_rig.auth)
    unknown = admin_rig.client.get("/admin/api/captures?decision=guessed", headers=admin_rig.auth)
    detail = admin_rig.client.get(f"/admin/api/captures/{classified['id']}", headers=admin_rig.auth)

    assert [row["id"] for row in listing.json()["captures"]] == [classified["id"]]
    assert unknown.status_code == 400
    assert detail.json()["layer_probabilities"] == {"finance": 0.9, "eng": 0.1}
    assert queue.get_submission_trace(admin_rig.conn, classified["id"])["layer_decision"] == schema.CLASSIFIED


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
    assert "exactly one layer" in empty_audience.json()["error"]
    assert unknown_audience.status_code == 400
    assert "not a member of" in unknown_audience.json()["error"]


def test_an_old_rebuild_of_an_index_that_has_not_changed_since_is_not_a_warning(admin_rig):
    """A writer asleep for days indexes nothing; its last full rebuild being old says nothing about
    drift, and the warning once fired regardless."""
    with admin_rig.conn.cursor() as cursor:
        cursor.execute(
            "UPDATE index_health SET dirty = FALSE, dirty_since = NULL, "
            "last_full_rebuild_at = now() - interval '3 days', "
            "last_incremental_at = now() - interval '4 days' WHERE singleton"
        )

    response = admin_rig.client.get("/admin/api/index", headers=admin_rig.auth)

    assert response.json()["warnings"] == []


def test_without_the_landing_the_bare_host_opens_the_backoffice(monkeypatch):
    """The landing is the hosted service's marketing page; a deployment that has not turned it on
    sends its visitors to sign in."""
    monkeypatch.delenv(routes.LANDING_ENV, raising=False)
    client = TestClient(routes._Branch(Starlette(), Starlette()))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302 and response.headers["location"] == "/admin/"
    assert client.get("/sub-processors", follow_redirects=False).status_code == 404


def test_a_landing_turned_on_but_never_built_opens_the_backoffice(monkeypatch, tmp_path):
    """The platform ships no landing page; turning it on without building one must not break the
    bare host."""
    monkeypatch.setenv(routes.LANDING_ENV, "on")
    monkeypatch.setattr(routes, "STATIC_DIR", tmp_path)
    client = TestClient(routes._Branch(Starlette(), Starlette()))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302 and response.headers["location"] == "/admin/"
