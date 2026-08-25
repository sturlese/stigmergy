import asyncio
import dataclasses
import datetime as dt
import gc
import json
import subprocess
import tracemalloc

import pytest
from pydantic import ValidationError

from stigmergy.capture import queue, schema
from stigmergy.capture.errors import ArtifactRejected, QueueStateError
from stigmergy.capture.source import source_path
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.knowledge.plan import FilingPlan
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.sources import parse_source
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker
from stigmergy.slack import copy, snapshot
from stigmergy.slack.capture import (
    PROFILE_LOOKUP_CONCURRENCY,
    handle_reaction_added,
)
from stigmergy.slack.gateway import FakeSlackGateway, SlackApiError
from stigmergy.slack.identity import ForeignTeam, Ignored, NoAccess, Resolved, TransientFailure
from stigmergy.slack.snapshot import (
    SlackSnapshot,
    SnapshotMessage,
    canonical_bytes,
    validate_snapshot,
)
from tests.knowledge.conftest import target_repo as make_target_repo
from tests.server.conftest import make_service
from tests.slack.conftest import (
    FINANCE_CHANNEL,
    PUBLIC_CHANNEL,
    TEAM_ID,
    UNLISTED_CHANNEL,
    build_context,
)

pytestmark = pytest.mark.timeout(30)


@pytest.fixture(name="target_repo")
def _target_repo(tmp_path):
    return make_target_repo.__wrapped__(tmp_path)


def run(coroutine):
    return asyncio.run(coroutine)


def seed_thread(gateway, channel, thread_ts="100.1", *, attachments=()):
    gateway.seed_channel(channel, name="finance-team")
    gateway.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    messages = [
        {
            "ts": thread_ts,
            "thread_ts": thread_ts,
            "user": "U_ANA",
            "text": "we should track this",
            "files": list(attachments),
        },
        {
            "ts": "100.2",
            "thread_ts": thread_ts,
            "user": "U_ANA",
            "text": "decision: ship it Friday",
        },
    ]
    gateway.seed_thread(channel, thread_ts, messages)


def resolved(subject="ana@example.com", audiences=frozenset({"finance"})):
    return Resolved(email=subject, audiences=audiences)


def capture(ctx, *, channel=FINANCE_CHANNEL, thread_ts="100.1", user="U_ANA", identity=None):
    return run(
        handle_reaction_added(
            ctx,
            reaction="brain",
            team_id=TEAM_ID,
            channel_id=channel,
            message_ts=thread_ts,
            slack_user_id=user,
            identity_result=identity or resolved(),
        )
    )


def queue_rows(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, operation, request, submitted_by, actor, acl, status FROM capture_queue ORDER BY created_at, id"
        )
        return cursor.fetchall()


class CountingGateway(FakeSlackGateway):
    def __init__(self):
        super().__init__()
        self.downloads = 0

    async def download_file(self, url, *, max_bytes):
        self.downloads += 1
        return await super().download_file(url, max_bytes=max_bytes)


class RetentionMeasuringGateway(CountingGateway):
    """Samples live allocations before each distinct Slack download returns."""

    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.live_bytes = []

    async def download_file(self, url, *, max_bytes):
        self.downloads += 1
        gc.collect()
        self.live_bytes.append(tracemalloc.get_traced_memory()[0])
        return bytes(bytearray(self.payload))


def test_brain_reaction_queues_one_normalized_capture_with_canonical_snapshot(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True

    rows = queue_rows(conn)
    assert len(rows) == 1
    capture_id, operation, request, submitted_by, actor, acl, status = rows[0]
    assert operation == schema.CAPTURE
    assert submitted_by == "ana@example.com"
    assert actor == {"subject": "ana@example.com", "display_name": "Ana"}
    assert acl == ["finance"]
    assert status == schema.QUEUED
    assert request["capture_id"] == str(capture_id)
    assert request["origin"]["adapter"] == "slack"
    assert request["origin"]["acquisition"] is None
    assert "metadata" not in request["origin"]
    assert len(request["artifacts"]) == 1
    snapshot_bytes = ctx.evidence.get(request["artifacts"][0]["blob_ref"])
    snapshot = validate_snapshot(snapshot_bytes)
    assert [message.text for message in snapshot.messages] == [
        "we should track this",
        "decision: ship it Friday",
    ]
    assert json.dumps(request).find("decision: ship it Friday") == -1
    assert len(gateway.posted) == 1
    assert "queued and attributed to Ana" in gateway.posted[0].text


def test_single_message_uses_the_same_snapshot_contract(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gateway.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    gateway.seed_thread(
        FINANCE_CHANNEL,
        "200.1",
        [
            {"ts": "200.1", "user": "U_ANA", "text": "one-off note"},
        ],
    )
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx, thread_ts="200.1") is True

    request = queue_rows(conn)[0][2]
    snapshot = validate_snapshot(ctx.evidence.get(request["artifacts"][0]["blob_ref"]))
    assert len(snapshot.messages) == 1
    assert snapshot.messages[0].text == "one-off note"


def test_reacting_to_a_reply_captures_its_complete_thread(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert (
        run(
            handle_reaction_added(
                ctx,
                reaction="brain",
                team_id=TEAM_ID,
                channel_id=FINANCE_CHANNEL,
                message_ts="100.2",
                slack_user_id="U_ANA",
                identity_result=resolved(),
            )
        )
        is True
    )

    request = queue_rows(conn)[0][2]
    snapshot = validate_snapshot(ctx.evidence.get(request["artifacts"][0]["blob_ref"]))
    assert snapshot.thread_ts == "100.1"
    assert [message.ts for message in snapshot.messages] == ["100.1", "100.2"]
    assert snapshot.permalink.endswith("p1001")
    assert snapshot.messages[0].permalink.endswith("p1001")
    assert snapshot.messages[1].permalink.endswith("p1002")
    assert gateway.posted[0].thread_ts == "100.1"


def test_max_thread_bounds_profile_concurrency_and_uses_one_permalink_call(indexed, clean_tables):
    class BoundedGateway(FakeSlackGateway):
        def __init__(self):
            super().__init__()
            self.active_profiles = 0
            self.max_active_profiles = 0
            self.profile_calls = 0
            self.permalink_calls = 0

        async def users_info(self, user_id):
            self.profile_calls += 1
            self.active_profiles += 1
            self.max_active_profiles = max(self.max_active_profiles, self.active_profiles)
            try:
                await asyncio.sleep(0.001)
                return await super().users_info(user_id)
            finally:
                self.active_profiles -= 1

        async def get_permalink(self, channel_id, message_ts):
            self.permalink_calls += 1
            return await super().get_permalink(channel_id, message_ts)

    conn, fixture = indexed
    gateway = BoundedGateway()
    thread_ts = "300.000001"
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team")
    messages = []
    for index in range(snapshot.MAX_THREAD_MESSAGES):
        user_id = f"U_{index:03d}"
        gateway.seed_user(user_id, None, display_name=f"User {index}")
        messages.append(
            {
                "ts": f"300.{index + 1:06d}",
                "thread_ts": thread_ts,
                "user": user_id,
                "text": f"Message {index}",
            }
        )
    gateway.seed_thread(FINANCE_CHANNEL, thread_ts, messages)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx, thread_ts=thread_ts, user="U_000") is True
    assert gateway.profile_calls == snapshot.MAX_THREAD_MESSAGES
    assert 1 < gateway.max_active_profiles <= PROFILE_LOOKUP_CONCURRENCY
    assert gateway.permalink_calls == 1


def test_acquisition_deadline_releases_the_reservation_for_retry(indexed, clean_tables, monkeypatch):
    class StallingGateway(FakeSlackGateway):
        def __init__(self):
            super().__init__()
            self.stall = True

        async def conversations_replies(self, channel_id, thread_ts):
            if self.stall:
                self.stall = False
                await asyncio.sleep(60)
            return await super().conversations_replies(channel_id, thread_ts)

    monkeypatch.setattr("stigmergy.slack.capture.CAPTURE_ACQUISITION_TIMEOUT_S", 0.1)
    conn, fixture = indexed
    gateway = StallingGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is False
    assert queue_rows(conn) == []
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM slack_submissions")
        assert cursor.fetchone()[0] == 0

    assert capture(ctx) is True
    assert len(queue_rows(conn)) == 1


def test_slack_attachments_are_exact_additional_artifacts(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    attachment = {
        "id": "F1",
        "name": "decision.txt",
        "mimetype": "text/plain",
        "url_private_download": "https://slack.local/F1",
    }
    gateway.seed_file(attachment["url_private_download"], b"exact attachment bytes")
    seed_thread(gateway, FINANCE_CHANNEL, attachments=[attachment])
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True

    request = queue_rows(conn)[0][2]
    assert len(request["artifacts"]) == 2
    snapshot = validate_snapshot(ctx.evidence.get(request["artifacts"][0]["blob_ref"]))
    assert snapshot.messages[0].attachments[0].artifact_index == 2
    second = request["artifacts"][1]
    assert second["original_name"] == "decision.txt"
    assert ctx.evidence.get(second["blob_ref"]) == b"exact attachment bytes"


def test_identical_attachments_at_the_budget_boundary_share_one_artifact(indexed, clean_tables, monkeypatch):
    """A repeated Slack upload cannot consume a second artifact or byte-budget allocation."""
    monkeypatch.setattr(schema, "MAX_CAPTURE_BYTES", 3_000, raising=False)
    conn, fixture = indexed
    gateway = CountingGateway()
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gateway.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    payload = b"x" * 1_500
    url = "https://slack.local/F-boundary"
    attachment = {
        "id": "F-boundary",
        "name": "decision.txt",
        "mimetype": "text/plain",
        "url_private_download": url,
    }
    gateway.seed_file(url, payload)
    gateway.seed_thread(
        FINANCE_CHANNEL,
        "100.1",
        [
            {
                "ts": "100.1",
                "thread_ts": "100.1",
                "user": "U_ANA",
                "text": "First message carries the file.",
                "files": [attachment],
            },
            {
                "ts": "100.2",
                "thread_ts": "100.1",
                "user": "U_ANA",
                "text": "Slack repeats that exact file on the reply.",
                "files": [attachment],
            },
        ],
    )
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True

    request = queue_rows(conn)[0][2]
    assert len(request["artifacts"]) == 2
    captured_snapshot = validate_snapshot(ctx.evidence.get(request["artifacts"][0]["blob_ref"]))
    assert [message.attachments[0].artifact_index for message in captured_snapshot.messages] == [2, 2]
    assert request["artifacts"][1]["bytes"] == len(payload)
    assert gateway.downloads == 1


def test_unknown_duplicate_attachments_consume_one_bounded_acquisition_each(indexed, clean_tables):
    conn, fixture = indexed
    gateway = CountingGateway()
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gateway.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    first_url = "https://slack.local/F-unknown-1"
    second_url = "https://slack.local/F-unknown-2"
    gateway.seed_file(first_url, b"same bytes")
    gateway.seed_file(second_url, b"same bytes")
    gateway.seed_thread(
        FINANCE_CHANNEL,
        "100.1",
        [
            {
                "ts": "100.1",
                "thread_ts": "100.1",
                "user": "U_ANA",
                "text": "first",
                "files": [
                    {
                        "id": "F-unknown-1",
                        "name": "one.txt",
                        "mimetype": "text/plain",
                        "url_private_download": first_url,
                    }
                ],
            },
            {
                "ts": "100.2",
                "thread_ts": "100.1",
                "user": "U_ANA",
                "text": "second",
                "files": [
                    {
                        "id": "F-unknown-2",
                        "name": "two.txt",
                        "mimetype": "text/plain",
                        "url_private_download": second_url,
                    }
                ],
            },
        ],
    )

    assert capture(build_context(fixture, conn, gateway=gateway)) is True
    assert gateway.downloads == 2
    assert len(queue_rows(conn)[0][2]["artifacts"]) == 2


def test_high_volume_unknown_duplicate_attachments_cannot_exceed_capture_work_budget(
    indexed, clean_tables
):
    conn, fixture = indexed
    gateway = CountingGateway()
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gateway.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    messages = []
    for index in range(schema.MAX_ARTIFACTS + 5):
        url = f"https://slack.local/F-repeat-{index}"
        gateway.seed_file(url, b"repeated bytes")
        messages.append(
            {
                "ts": f"100.{index + 1}",
                "thread_ts": "100.1",
                "user": "U_ANA",
                "text": "Slack repeats an unknown upload.",
                "files": [
                    {
                        "id": f"F-repeat-{index}",
                        "name": f"repeat-{index}.txt",
                        "mimetype": "text/plain",
                        "url_private_download": url,
                    }
                ],
            }
        )
    gateway.seed_thread(FINANCE_CHANNEL, "100.1", messages)

    assert capture(build_context(fixture, conn, gateway=gateway)) is False
    assert gateway.downloads == schema.MAX_ARTIFACTS - 1
    assert queue_rows(conn) == []


def test_distinct_attachment_keys_with_one_digest_do_not_retain_bytes_per_key(indexed, clean_tables):
    """Deduplication may retain one artifact, never one raw byte buffer per stable Slack key."""
    conn, fixture = indexed
    payload = b"x" * (128 * 1024)
    gateway = RetentionMeasuringGateway(payload)
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gateway.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    messages = []
    for index in range(schema.MAX_ARTIFACTS - 1):
        messages.append(
            {
                "ts": f"100.{index + 1}",
                "thread_ts": "100.1",
                "user": "U_ANA",
                "text": "Slack repeats equivalent bytes under a distinct file key.",
                "files": [
                    {
                        "id": f"F-retention-{index}",
                        "name": f"retention-{index}.txt",
                        "mimetype": "text/plain",
                        "url_private_download": f"https://slack.local/F-retention-{index}",
                    }
                ],
            }
        )
    gateway.seed_thread(FINANCE_CHANNEL, "100.1", messages)

    tracemalloc.start()
    try:
        assert capture(build_context(fixture, conn, gateway=gateway)) is True
    finally:
        tracemalloc.stop()

    assert gateway.downloads == schema.MAX_ARTIFACTS - 1
    assert len(queue_rows(conn)[0][2]["artifacts"]) == 2
    assert max(gateway.live_bytes) - min(gateway.live_bytes) < len(payload) * 3


def test_recovered_slack_capture_keeps_its_archived_source_readable(
    indexed, clean_tables, monkeypatch, target_repo
):
    """Acknowledgement loss must recover the committed Slack source without rewriting it."""
    identities_path = target_repo / "ops" / "identities.json"
    identities = json.loads(identities_path.read_text(encoding="utf-8"))
    identities["ana@example.com"] = {
        "display_name": "Ana",
        "groups": ["finance"],
        "default_audience": ["finance"],
    }
    identities_path.write_text(json.dumps(identities, sort_keys=True) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "ops/identities.json"], cwd=target_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "add Slack actor"],
        cwd=target_repo,
        check=True,
    )
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    attachment = {
        "id": "F-recovery",
        "name": "decision.txt",
        "mimetype": "text/plain",
        "url_private_download": "https://slack.local/F-recovery",
    }
    gateway.seed_file(attachment["url_private_download"], b"The attachment preserves the decision.")
    seed_thread(gateway, FINANCE_CHANNEL, attachments=[attachment])
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True
    capture_id, _operation, request, *_rest = queue_rows(conn)[0]
    archived_path = source_path(schema.parse_capture(request))
    deps = WriterDeps(
        config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
        ctx.evidence,
        ScriptedPlanner(FilingPlan(summary="Archived Slack evidence")),
        str(target_repo),
    )
    original_finish_landed = queue.finish_landed

    def acknowledgement_loss(*_args, **_kwargs):
        raise QueueStateError("simulated acknowledgement loss")

    monkeypatch.setattr(queue, "finish_landed", acknowledgement_loss)
    with pytest.raises(QueueStateError, match="acknowledgement loss"):
        worker.process_next(conn, deps)
    monkeypatch.setattr(queue, "finish_landed", original_finish_landed)
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE capture_queue SET status = 'queued', processing_started_at = NULL, "
            "next_attempt_at = now() WHERE id = %s",
            (capture_id,),
        )

    item, outcome = worker.process_next(conn, deps)

    assert outcome.status == schema.LANDED
    assert item["source_path"] == archived_path
    source_text = subprocess.check_output(
        ["git", "show", f"main:{archived_path}"], cwd=target_repo, text=True
    )
    archived = parse_source(archived_path, source_text)
    assert archived.title == "Slack thread in #finance-team"
    assert "decision: ship it Friday" in archived.body
    assert "Attachment 2: decision.txt" in archived.body

    build.rebuild(conn, str(target_repo), build_embedder("fake"))
    in_scope = make_service(fixture, conn, fixture.ANA).read_page(archived_path)
    out_of_scope = make_service(fixture, conn, fixture.ENG).read_page(archived_path)

    assert "body" in in_scope
    assert "decision: ship it Friday" in in_scope["body"]
    assert out_of_scope == {"error": f"unknown page: {archived_path}"}

    # This module shares one index connection; restore its mapped-channel controls for later tests.
    build.rebuild(conn, fixture.repo, build_embedder("fake"))


def test_snapshot_plus_nineteen_attachments_is_the_hard_limit(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    attachments = []
    for index in range(19):
        url = f"https://slack.local/F{index}"
        attachments.append(
            {
                "id": f"F{index}",
                "name": f"file-{index}.txt",
                "mimetype": "text/plain",
                "url_private_download": url,
            }
        )
        gateway.seed_file(url, f"file {index}".encode())
    seed_thread(gateway, FINANCE_CHANNEL, attachments=attachments)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True
    assert len(queue_rows(conn)[0][2]["artifacts"]) == schema.MAX_ARTIFACTS


def test_snapshot_schema_rejects_threads_over_the_message_limit():
    with pytest.raises(ValidationError):
        SlackSnapshot(
            team_id="T1",
            channel_id="C1",
            channel_name="product",
            thread_ts="1.0",
            permalink="https://example.slack.com/thread",
            messages=tuple(
                SnapshotMessage(
                    order=index + 1,
                    ts=f"{index + 1}.0",
                    occurred_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
                    user_id="U1",
                    speaker="Alice",
                    text="message",
                    permalink="https://example.slack.com/thread",
                )
                for index in range(snapshot.MAX_THREAD_MESSAGES + 1)
            ),
        )


def test_snapshot_schema_rejects_bytes_over_the_snapshot_limit(monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_BYTES", 200, raising=False)
    value = SlackSnapshot(
        team_id="T1",
        channel_id="C1",
        channel_name="product",
        thread_ts="1.0",
        permalink="https://example.slack.com/thread",
        messages=(
            SnapshotMessage(
                order=1,
                ts="1.0",
                occurred_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
                user_id="U1",
                speaker="Alice",
                text="x" * 500,
                permalink="https://example.slack.com/thread",
            ),
        ),
    )

    with pytest.raises(ArtifactRejected):
        canonical_bytes(value)


def test_new_slack_attachment_is_rejected_after_per_file_acquisition(indexed, clean_tables, monkeypatch):
    class BudgetGateway(FakeSlackGateway):
        def __init__(self):
            super().__init__()
            self.download_limits = []

        async def download_file(self, url, *, max_bytes):
            self.download_limits.append(max_bytes)
            return await super().download_file(url, max_bytes=max_bytes)

    monkeypatch.setattr(schema, "MAX_CAPTURE_BYTES", 1_000, raising=False)
    conn, fixture = indexed
    gateway = BudgetGateway()
    attachment = {
        "id": "F-budget",
        "name": "budget.txt",
        "mimetype": "text/plain",
        "url_private_download": "https://slack.local/F-budget",
    }
    gateway.seed_file(attachment["url_private_download"], b"x" * 1_000)
    seed_thread(gateway, FINANCE_CHANNEL, attachments=[attachment])
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is False
    assert gateway.download_limits == [schema.MAX_ARTIFACT_BYTES]
    assert queue_rows(conn) == []


def test_twentieth_attachment_is_refused_without_a_queue_row(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    attachments = []
    for index in range(20):
        url = f"https://slack.local/F{index}"
        attachments.append(
            {
                "id": f"F{index}",
                "name": f"file-{index}.txt",
                "mimetype": "text/plain",
                "url_private_download": url,
            }
        )
        gateway.seed_file(url, f"file {index}".encode())
    seed_thread(gateway, FINANCE_CHANNEL, attachments=attachments)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is False
    assert queue_rows(conn) == []
    assert gateway.ephemeral[0].text == copy.CAPTURE_FAILED


def test_same_reactor_and_thread_is_idempotent(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True
    assert capture(ctx) is False
    assert len(queue_rows(conn)) == 1
    assert len(gateway.posted) == 1


def test_duplicate_reaction_is_reserved_before_thread_acquisition(indexed, clean_tables):
    class CountingGateway(FakeSlackGateway):
        def __init__(self):
            super().__init__()
            self.reply_calls = 0
            self.download_calls = 0

        async def conversations_replies(self, channel_id, thread_ts):
            self.reply_calls += 1
            return await super().conversations_replies(channel_id, thread_ts)

        async def download_file(self, url, *, max_bytes):
            self.download_calls += 1
            return await super().download_file(url, max_bytes=max_bytes)

    conn, fixture = indexed
    gateway = CountingGateway()
    attachment = {
        "id": "F-dedup",
        "name": "decision.txt",
        "mimetype": "text/plain",
        "url_private_download": "https://slack.local/F-dedup",
    }
    gateway.seed_file(attachment["url_private_download"], b"decision")
    seed_thread(gateway, FINANCE_CHANNEL, attachments=[attachment])
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True
    assert capture(ctx) is False

    assert gateway.reply_calls == 1
    assert gateway.download_calls == 1


def test_different_reactors_create_distinct_attributed_captures(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    gateway.seed_user("U_STEWARD", "steward@example.com", display_name="Steward")
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True
    assert (
        capture(
            ctx,
            user="U_STEWARD",
            identity=resolved("steward@example.com", None),
        )
        is True
    )

    assert {row[3] for row in queue_rows(conn)} == {"ana@example.com", "steward@example.com"}


def test_non_brain_reaction_is_ignored(indexed, clean_tables):
    conn, fixture = indexed
    ctx = build_context(fixture, conn, gateway=FakeSlackGateway())
    result = run(
        handle_reaction_added(
            ctx,
            reaction="thumbsup",
            team_id=TEAM_ID,
            channel_id=FINANCE_CHANNEL,
            message_ts="1.1",
            slack_user_id="U_ANA",
            identity_result=resolved(),
        )
    )
    assert result is False
    assert queue_rows(conn) == []


@pytest.mark.parametrize("identity", [Ignored("bot"), ForeignTeam("other")])
def test_ignored_identity_produces_no_slack_traffic(indexed, clean_tables, identity):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx, identity=identity) is False
    assert queue_rows(conn) == []
    assert gateway.posted == [] and gateway.ephemeral == []


def test_identity_refusals_are_private_and_distinct(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx, identity=NoAccess()) is False
    assert gateway.ephemeral[-1].text == copy.no_access(is_dm=False)
    assert capture(ctx, identity=TransientFailure("timeout")) is False
    assert gateway.ephemeral[-1].text == copy.TRANSIENT_IDENTITY_FAILURE
    assert queue_rows(conn) == []


def test_unmapped_channel_fails_safely_even_when_public(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, UNLISTED_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx, channel=UNLISTED_CHANNEL) is False
    assert queue_rows(conn) == []
    assert gateway.ephemeral[0].text == copy.PRIVATE_CHANNEL_REFUSAL


def test_explicit_public_channel_is_organization_wide(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, PUBLIC_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx, channel=PUBLIC_CHANNEL) is True
    assert queue_rows(conn)[0][5] is None


def test_mapped_private_channel_uses_its_configured_audience(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team", is_private=True)
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True
    assert queue_rows(conn)[0][5] == ["finance"]


@pytest.mark.parametrize("channel_flag", ["is_im", "is_mpim"])
def test_direct_messages_are_not_capture_channels(indexed, clean_tables, channel_flag):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    gateway.seed_channel(FINANCE_CHANNEL, name="dm", **{channel_flag: True})
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is False
    assert queue_rows(conn) == []


def test_reactor_outside_channel_audience_is_refused_before_reservation(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    gateway.seed_user("U_ENG", "eng@example.com", display_name="Engineer")
    ctx = build_context(fixture, conn, gateway=gateway)

    assert (
        capture(
            ctx,
            user="U_ENG",
            identity=resolved("eng@example.com", frozenset({"eng"})),
        )
        is False
    )
    assert queue_rows(conn) == []
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM slack_submissions")
        assert cursor.fetchone()[0] == 0


def test_queue_failure_releases_reservation_for_a_real_retry(indexed, clean_tables):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    good = build_context(fixture, conn, gateway=gateway)
    broken = dataclasses.replace(good, evidence=None)

    assert capture(broken) is False
    assert queue_rows(conn) == []
    assert gateway.ephemeral[-1].text == copy.CAPTURE_FAILED
    assert capture(good) is True
    assert len(queue_rows(conn)) == 1


def test_attach_failure_rolls_back_queue_and_reservation(indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    def fail(*args, **kwargs):
        raise RuntimeError("attach failed")

    monkeypatch.setattr("stigmergy.slack.capture.attach_submission", fail)

    assert capture(ctx) is False
    assert queue_rows(conn) == []
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM slack_submissions")
        assert cursor.fetchone()[0] == 0


def test_slack_acquisition_failure_is_reported_privately(indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    seed_thread(gateway, FINANCE_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gateway)

    async def fail(*args, **kwargs):
        raise SlackApiError("unavailable")

    monkeypatch.setattr(gateway, "conversations_replies", fail)

    assert capture(ctx) is False
    assert queue_rows(conn) == []
    assert gateway.ephemeral[-1].text == copy.CAPTURE_FAILED


def test_identical_attachment_bytes_across_messages_are_one_artifact(indexed, clean_tables):
    """Slack lists one upload under every message it reached; the capture keeps each message's
    reference but extracts the bytes once."""
    conn, fixture = indexed
    gateway = FakeSlackGateway()
    gateway.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gateway.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    attachment = {
        "id": "F1",
        "name": "order.txt",
        "mimetype": "text/plain",
        "url_private_download": "https://slack.local/F1",
    }
    gateway.seed_file(attachment["url_private_download"], b"the same order bytes")
    gateway.seed_thread(
        FINANCE_CHANNEL,
        "100.1",
        [
            {"ts": "100.1", "thread_ts": "100.1", "user": "U_ANA", "text": "attach it here", "files": [attachment]},
            {"ts": "100.2", "thread_ts": "100.1", "user": "U_ANA", "text": "and it landed here", "files": [attachment]},
        ],
    )
    ctx = build_context(fixture, conn, gateway=gateway)

    assert capture(ctx) is True

    request = queue_rows(conn)[0][2]
    assert len(request["artifacts"]) == 2
    parsed = validate_snapshot(ctx.evidence.get(request["artifacts"][0]["blob_ref"]))
    assert [item.artifact_index for item in parsed.messages[0].attachments] == [2]
    assert [item.artifact_index for item in parsed.messages[1].attachments] == [2]
    assert snapshot.render_snapshot(parsed).count("### Attachment 2: order.txt") == 2
    assert ctx.evidence.get(request["artifacts"][1]["blob_ref"]) == b"the same order bytes"
