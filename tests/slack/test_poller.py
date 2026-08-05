"""The push channel: filed / needs_input / triage / rejected / resolved / failed — every terminal
and parked state gets the same treatment, `failed` included.
"""
import asyncio

import pytest

from stigmergy.capture import dispositions, queue
from stigmergy.capture import schema as capture_schema
from stigmergy.librarian import report
from stigmergy.slack import copy, poller
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.store import attach_submission, reserve
from tests.slack.conftest import TEAM_ID, build_context

pytestmark = pytest.mark.timeout(30)


def _run(coro):
    return asyncio.run(coro)


def _new_submission(ctx, *, identity: str, channel_id: str, thread_ts: str) -> int:
    service = ctx.build_service(identity, None)
    ack = service.submit("raw", f"material for {thread_ts}")
    submission_id = ack["id"]
    reservation_id = reserve(ctx.conn, team_id=TEAM_ID, channel_id=channel_id, message_ts=thread_ts,
                             thread_ts=thread_ts, slack_user_id="U1", submitted_by=identity)
    attach_submission(ctx.conn, reservation_id, submission_id)
    return submission_id


def _claim_and_finish(conn, submission_id, *, status, report_dict, result_ref=""):
    claimed = queue.claim_next(conn)
    assert claimed["id"] == submission_id
    queue.finish(conn, submission_id, status=status, expected_attempts=claimed["attempts"],
                result_ref=result_ref, error=report_dict["summary"], report=report_dict)


def test_filed_reports_the_page_commit_and_anchor(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="1.1")
    rep = report.filed(page_path="wiki/entities/acme.md", commit="deadbeef",
                       anchoring={"entities": ["Acme Corp"]}, links=[], overlaps=[],
                       findings=[])
    _claim_and_finish(conn, submission_id, status=capture_schema.FILED, report_dict=rep,
                      result_ref="wiki/entities/acme.md@deadbeef")

    reported = _run(poller.poll_once(ctx))

    assert reported == 1
    text = gw.posted[0].blocks[0]["text"]["text"]
    assert "`wiki/entities/acme.md`" in text
    assert "`deadbeef`" in text
    assert "Acme Corp" in text
    assert gw.posted[0].thread_ts == "1.1"
    # An ordinary filing carries no source page, and the card must not invent one.
    assert "archived word-for-word" not in text


def test_filed_with_a_source_page_names_the_verbatim_thread_copy_on_the_card(indexed,
                                                                             clean_tables):
    """A 🧠 capture files a `sources/slack/` page beside the synthesis, and the person
    who reacted learns BOTH pages exist from the card — part 1 only, the head of the chain, the
    same page the synthesis's `sources:` cites."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="2.1")
    rep = report.filed(page_path="wiki/notes/acme-renewal.md", commit="deadbeef",
                       anchoring={"entities": ["Acme Corp"]}, links=[], overlaps=[],
                       findings=[],
                       source_pages=["sources/slack/acme-renewal-thread.md",
                                     "sources/slack/acme-renewal-thread-p2.md"])
    _claim_and_finish(conn, submission_id, status=capture_schema.FILED, report_dict=rep,
                      result_ref="wiki/notes/acme-renewal.md@deadbeef")

    reported = _run(poller.poll_once(ctx))

    assert reported == 1
    text = gw.posted[0].blocks[0]["text"]["text"]
    assert "`wiki/notes/acme-renewal.md`" in text
    assert "archived word-for-word at `sources/slack/acme-renewal-thread.md`" in text
    assert "acme-renewal-thread-p2" not in text        # one door into the chain, not the list


def test_needs_input_addresses_the_submitter_and_swaps_the_mcp_invocation(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="2.1")
    rep = report.needs_input(submission_id=submission_id, name="Acme",
                             candidates=[{"name": "Acme Corp", "aliases": ["Acme"]}])
    _claim_and_finish(conn, submission_id, status=capture_schema.NEEDS_INPUT, report_dict=rep)

    reported = _run(poller.poll_once(ctx))

    assert reported == 1
    text = gw.posted[0].blocks[0]["text"]["text"]
    assert text.startswith("<@U1> —")
    assert copy.NEEDS_INPUT_INSTRUCTION in text
    assert "brain_reply(" not in text
    assert "Acme Corp" in text   # the situation prose is reused, not rewritten


@pytest.mark.parametrize("status,rep_builder", [
    (capture_schema.TRIAGE, lambda: report.triage_entity(name="Acme")),
    (capture_schema.REJECTED, lambda: report.rejected_duplicate(page_path="x.md", as_of="2026-01")),
    (capture_schema.FAILED, lambda: report.failed_system(attempts=1, stage="gate",
                                                         reason="zone refused")),
])
def test_generic_reports_are_bold_prefixed_and_reuse_the_sentence_verbatim(
        indexed, clean_tables, status, rep_builder):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="3.1")
    rep = rep_builder()
    _claim_and_finish(conn, submission_id, status=status, report_dict=rep)

    reported = _run(poller.poll_once(ctx))

    assert reported == 1
    text = gw.posted[0].blocks[0]["text"]["text"]
    assert text.startswith(f"*{status}* —")
    assert f"{status} — {status}" not in text


def test_resolved_report_reuses_the_stewards_own_sentence(indexed, clean_tables):
    """`resolved` is reachable only through `capture.dispositions.resolve` (a steward's
    disposition on a PARKED row, via `queue.dispose` — never through the lease-fenced
    `queue.finish`), so the row is parked into `triage` first, exactly as a real one would be."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="4.1")
    _claim_and_finish(conn, submission_id, status=capture_schema.TRIAGE,
                      report_dict=report.triage_entity(name="Acme"))
    dispositions.resolve(conn, submission_id, actor="steward", note="handled by hand")

    reported = _run(poller.poll_once(ctx))

    assert reported == 1
    text = gw.posted[0].blocks[0]["text"]["text"]
    assert text.startswith("*resolved* —")
    assert "handled by hand" in text


def test_a_status_is_reported_exactly_once_even_across_multiple_polls(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="5.1")
    rep = report.triage_entity(name="Acme")
    _claim_and_finish(conn, submission_id, status=capture_schema.TRIAGE, report_dict=rep)

    first = _run(poller.poll_once(ctx))
    second = _run(poller.poll_once(ctx))

    assert first == 1
    assert second == 0
    assert len(gw.posted) == 1


def test_a_submission_with_no_slack_origin_produces_no_slack_traffic(indexed, clean_tables):
    """The card names the parked status without echoing the material behind it."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    service = ctx.build_service(fixture.STEWARD, None)
    ack = service.submit("raw", "an MCP-originated capture")   # no slack_submissions row at all
    claimed = queue.claim_next(conn)
    assert claimed["id"] == ack["id"]
    rep = report.triage_entity(name="Someone")
    queue.finish(conn, ack["id"], status=capture_schema.TRIAGE, expected_attempts=claimed["attempts"],
                error=rep["summary"], report=rep)

    reported = _run(poller.poll_once(ctx))

    assert reported == 0
    assert gw.posted == []
