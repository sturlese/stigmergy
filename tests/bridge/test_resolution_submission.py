"""Resolution target preservation across the official local bridge."""
import asyncio
import json

import httpx
import pytest

from stigmergy.bridge.acquire import Acquirer
from stigmergy.bridge.cloud import CloudClient
from stigmergy.bridge.server import build_mcp
from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.uploads import staging_ref
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from tests.server import conftest as server_fixtures

CONTRADICTION_ID = "con_00000000-0000-4000-8000-000000000001"


@pytest.fixture(scope="session", name="fixture")
def _fixture(tmp_path_factory):
    return server_fixtures.fixture.__wrapped__(tmp_path_factory)


@pytest.fixture(scope="module", name="indexed")
def _indexed(fixture):
    yield from server_fixtures.indexed.__wrapped__(fixture)


def test_local_brain_submit_exposes_and_forwards_resolution_target():
    class RecordingCloud:
        def __init__(self):
            self.metadata = None

        def submit_artifacts(self, artifacts, **metadata):
            self.metadata = metadata
            return {"id": "capture-1", "status": "queued"}

    cloud = RecordingCloud()
    mcp = build_mcp(cloud, Acquirer())
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert "resolution_of" in tools["brain_submit"].inputSchema["properties"]
    blocks, _ = asyncio.run(
        mcp.call_tool(
            "brain_submit",
            {"text": "Signed evidence confirms the annual term.",
             "resolution_of": CONTRADICTION_ID},
        )
    )

    assert json.loads(blocks[0].text)["status"] == "queued"
    assert cloud.metadata is not None
    assert cloud.metadata["resolution_of"] == CONTRADICTION_ID


def test_cloud_capture_finalize_preserves_resolution_target():
    observed = {}

    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/bridge/uploads":
            return httpx.Response(
                200,
                json={"upload_id": "upload-1", "upload_url": "", "expires_at": ""},
            )
        observed.update(body)
        return httpx.Response(200, json={"id": "capture-1", "status": "queued"})

    cloud = CloudClient(
        "https://brain.example",
        "member-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    cloud.submit_artifacts(
        [Acquirer().text("Signed evidence confirms the annual term.")],
        title=None,
        occurred_at=None,
        audience=None,
        resolution_of=CONTRADICTION_ID,
    )

    assert observed["resolution_of"] == CONTRADICTION_ID


def test_local_bridge_resolution_reaches_the_queued_capture_envelope(indexed):
    """The local tool, cloud request, and queue must preserve the resolution target together."""
    conn, fixture = indexed
    store = evidence.MemoryEvidenceStore()
    service = server_fixtures.make_service(fixture, conn, fixture.STEWARD, evidence=store)

    def handler(request):
        if request.method == "PUT" and request.url.host == "uploads.example":
            store.objects[staging_ref(request.url.path.rsplit("/", 1)[-1])] = request.content
            return httpx.Response(200)

        body = json.loads(request.content)
        if request.url.path == "/bridge/uploads":
            upload = service.create_upload(**body)
            upload["upload_url"] = f"https://uploads.example/{upload['upload_id']}"
            return httpx.Response(200, json=upload)
        if request.url.path == "/bridge/captures":
            return httpx.Response(200, json=service.finalize_upload_capture(**body))
        raise AssertionError(f"unexpected bridge request: {request.method} {request.url.path}")

    cloud = CloudClient(
        "https://brain.example",
        "member-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    mcp = build_mcp(cloud, Acquirer())
    blocks, _ = asyncio.run(
        mcp.call_tool(
            "brain_submit",
            {"text": "Signed evidence confirms the annual term.",
             "resolution_of": CONTRADICTION_ID},
        )
    )

    receipt = json.loads(blocks[0].text)
    assert receipt["status"] == schema.QUEUED
    item = queue.get_submission_trace(conn, receipt["id"])
    assert item is not None
    envelope = schema.parse_capture(item["request"])
    assert envelope.intent is not None
    assert envelope.intent.resolution_of == CONTRADICTION_ID


def test_local_bridge_audits_only_the_bounded_resolution_identifier(indexed):
    conn, fixture = indexed
    ensure_audit_table(conn)
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM audit_log")
    store = evidence.MemoryEvidenceStore()
    service = server_fixtures.make_service(
        fixture,
        conn,
        fixture.STEWARD,
        audit=AuditWriter(conn),
        evidence=store,
    )

    def handler(request):
        if request.method == "PUT" and request.url.host == "uploads.example":
            store.objects[staging_ref(request.url.path.rsplit("/", 1)[-1])] = request.content
            return httpx.Response(200)
        body = json.loads(request.content)
        if request.url.path == "/bridge/uploads":
            upload = service.create_upload(**body)
            upload["upload_url"] = f"https://uploads.example/{upload['upload_id']}"
            return httpx.Response(200, json=upload)
        if request.url.path == "/bridge/captures":
            return httpx.Response(200, json=service.finalize_upload_capture(**body))
        raise AssertionError(f"unexpected bridge request: {request.method} {request.url.path}")

    cloud = CloudClient(
        "https://brain.example",
        "member-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    blocks, _ = asyncio.run(
        build_mcp(cloud, Acquirer()).call_tool(
            "brain_submit",
            {"text": "Signed evidence confirms the annual term.", "resolution_of": CONTRADICTION_ID},
        )
    )

    assert json.loads(blocks[0].text)["status"] == schema.QUEUED
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT args FROM audit_log WHERE tool = 'brain_upload_finalize' "
            "ORDER BY id DESC LIMIT 1"
        )
        audit_args = cursor.fetchone()[0]
    assert audit_args == {"uploads": 1, "audience": None, "resolution_of": CONTRADICTION_ID}
