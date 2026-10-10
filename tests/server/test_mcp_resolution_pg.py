"""Real MCP coverage for explicit contradiction-resolution captures."""
import asyncio

from stigmergy.capture import queue, schema
from stigmergy.server.audit import ensure_audit_table
from tests.server.conftest import call_json, mcp_session

CONTRADICTION_ID = "con_00000000-0000-4000-8000-000000000001"


def test_brain_submit_over_stdio_persists_an_explicit_resolution_target(indexed):
    """The prescribed MCP capture path must carry the target into the writer contract."""
    conn, fixture = indexed

    async def submit_resolution():
        async with mcp_session(fixture, fixture.STEWARD) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert "resolution_of" in tools["brain_submit"].inputSchema["properties"]
            return await call_json(
                session,
                "brain_submit",
                text="Signed evidence confirms the annual term.",
                resolution_of=CONTRADICTION_ID,
            )

    receipt = asyncio.run(submit_resolution())

    assert receipt["status"] == schema.QUEUED
    item = queue.get_submission_trace(conn, receipt["id"])
    assert item is not None
    envelope = schema.parse_capture(item["request"])
    assert envelope.intent.resolution_of == CONTRADICTION_ID


def test_brain_submit_audits_only_the_bounded_resolution_identifier(indexed):
    conn, fixture = indexed
    ensure_audit_table(conn)
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM audit_log")

    async def submit_resolution():
        async with mcp_session(fixture, fixture.STEWARD) as session:
            return await call_json(
                session,
                "brain_submit",
                text="Signed evidence confirms the annual term.",
                resolution_of=CONTRADICTION_ID,
            )

    receipt = asyncio.run(submit_resolution())
    assert receipt["status"] == schema.QUEUED
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT args FROM audit_log WHERE tool = 'brain_submit' ORDER BY id DESC LIMIT 1"
        )
        audit_args = cursor.fetchone()[0]

    assert audit_args["resolution_of"] == CONTRADICTION_ID
    assert set(audit_args) == {"input", "text_bytes", "text_sha256", "audience", "resolution_of"}
