import datetime as dt
import hashlib
import json
from types import SimpleNamespace

from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.service import AdminService
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import evidence, queue, schema, uploads
from stigmergy.server.identity import Principal
from stigmergy.server.service import SLACK_DOOR, BrainService
from stigmergy.slack.snapshot import SlackSnapshot, SnapshotMessage, canonical_bytes


def _service(conn, store, principal, *, door=""):
    return BrainService(
        SimpleNamespace(identities_path="unused"),
        conn,
        None,
        audiences=None,
        identity=principal.subject,
        evidence=store,
        door=door,
        principal=principal,
    )


def test_mcp_slack_and_admin_enter_one_normalized_capture_queue(clean_queue, tmp_path):
    ensure_admin_schema(clean_queue)
    store = evidence.MemoryEvidenceStore()
    principal = Principal(
        subject="master@example.com",
        display_name="Master",
        groups=("brain-admins",),
        default_audience=None,
    )
    identity_path = tmp_path / "identities.json"
    identity_path.write_text(
        json.dumps(
            {
                principal.subject: {
                    "display_name": principal.display_name,
                    "groups": list(principal.groups),
                    "default_audience": None,
                }
            }
        ),
        encoding="utf-8",
    )

    bridge_bytes = b"Local bridge decision"
    digest = hashlib.sha256(bridge_bytes).hexdigest()
    bridge = _service(clean_queue, store, principal)
    upload = bridge.create_upload(
        idempotency_key="bridge-upload",
        sha256=digest,
        bytes=len(bridge_bytes),
        media_type=schema.MEDIA_TEXT,
        original_name="decision.txt",
    )
    store.objects[uploads.staging_ref(upload["upload_id"])] = bridge_bytes
    mcp_receipt = bridge.finalize_upload_capture(
        upload_ids=[upload["upload_id"]],
        idempotency_key="bridge-capture",
    )
    assert uploads.staging_ref(upload["upload_id"]) not in store.objects

    message_time = dt.datetime(2026, 8, 24, 10, tzinfo=dt.UTC)
    snapshot = canonical_bytes(
        SlackSnapshot(
            team_id="T1",
            channel_id="C1",
            channel_name="product",
            thread_ts="1787565600.000001",
            permalink="https://example.slack.com/thread",
            messages=(
                SnapshotMessage(
                    order=1,
                    ts="1787565600.000001",
                    occurred_at=message_time,
                    user_id="U1",
                    speaker="Master",
                    text="Slack decision",
                    permalink="https://example.slack.com/thread",
                ),
            ),
        )
    )
    slack_receipt = _service(
        clean_queue, store, principal, door=SLACK_DOOR
    ).submit_artifacts(
        artifact_values=((snapshot, schema.MEDIA_SLACK, "thread.json", None),),
        idempotency_key="slack-capture",
        audience=None,
    )

    admin_receipt = AdminService(
        clean_queue,
        server_settings=SimpleNamespace(identities_path=str(identity_path)),
        admin_settings=AdminSettings(actor=principal.subject),
        evidence=store,
    ).submit_text(
        text="Admin decision",
        title=None,
        occurred_at=None,
        audience=None,
        idempotency_key="admin-capture",
    )

    rows = [
        queue.get_submission_trace(clean_queue, receipt["id"])
        for receipt in (mcp_receipt, slack_receipt, admin_receipt)
    ]
    envelopes = [schema.parse_capture(row["request"]) for row in rows]

    assert {row["operation"] for row in rows} == {schema.CAPTURE}
    assert {envelope.origin.adapter for envelope in envelopes} == {"mcp", "slack", "admin"}
    assert {tuple(envelope.model_dump().keys()) for envelope in envelopes} == {
        (
            "capture_id",
            "idempotency_key",
            "actor",
            "audience",
            "origin",
            "artifacts",
            "intent",
        )
    }
    assert all(envelope.actor.subject == principal.subject for envelope in envelopes)
    assert all(envelope.artifacts and envelope.artifacts[0].blob_ref for envelope in envelopes)
