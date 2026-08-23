"""The push channel: filed / rejected / failed (and `resolved`, on rows from before captures
stopped parking) — every terminal state gets the same treatment, `failed` included, and nothing
is ever asked of the submitter.
"""
import asyncio

import pytest

from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.librarian import report
from stigmergy.slack import poller
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


@pytest.mark.parametrize("status,rep_builder", [
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


def test_a_legacy_resolved_row_still_reports_the_stewards_own_sentence(indexed, clean_tables):
    """`resolved` is a status nothing writes any more — a steward closed the row by hand back when
    captures could park — and a row carrying it still reports once, with the sentence the steward
    left. Written directly, the way such a row exists in a deployment."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="4.1")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capture_queue SET status = %s, finished_at = now(), report = %s WHERE id = %s",
            (capture_schema.RESOLVED,
             __import__("json").dumps({"status": capture_schema.RESOLVED,
                                       "summary": "resolved — handled by hand"}),
             submission_id))

    reported = _run(poller.poll_once(ctx))

    assert reported == 1
    text = gw.posted[0].blocks[0]["text"]["text"]
    assert text.startswith("*resolved* —")
    assert "handled by hand" in text


def test_the_filed_card_names_the_entity_the_capture_introduced(indexed, clean_tables):
    """The report's `entities_born` (what `report.filed` records when a capture introduced an
    identity for this capture) reaches the submitter's thread, with the promise that nothing waits
    on them."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="5.1")
    rep = report.filed(page_path="wiki/notes/Ledgerly kickoff.md", commit="abc1234",
                       anchoring={"kind": "entity", "entities": ["Ledgerly"], "reason": ""},
                       links=[], overlaps=[], findings=[],
                       entities_born=[{"id": "ledgerly", "name": "Ledgerly",
                                           "type": "organization"}])
    _claim_and_finish(conn, submission_id, status=capture_schema.FILED, report_dict=rep,
                      result_ref="wiki/notes/Ledgerly kickoff.md@abc1234")

    assert _run(poller.poll_once(ctx)) == 1
    text = gw.posted[0].blocks[0]["text"]["text"]
    assert "introduced *Ledgerly* as a new entity" in text
    assert "confirmed by you" in text


def test_a_status_is_reported_exactly_once_even_across_multiple_polls(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="5.1")
    rep = report.failed_system(attempts=1, stage="gate", reason="zone refused")
    _claim_and_finish(conn, submission_id, status=capture_schema.FAILED, report_dict=rep)

    first = _run(poller.poll_once(ctx))
    second = _run(poller.poll_once(ctx))

    assert first == 1
    assert second == 0
    assert len(gw.posted) == 1


def test_a_submission_with_no_slack_origin_produces_no_slack_traffic(indexed, clean_tables):
    """A capture with no Slack origin reaches a terminal state and no thread hears about it."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    service = ctx.build_service(fixture.STEWARD, None)
    ack = service.submit("raw", "an MCP-originated capture")   # no slack_submissions row at all
    claimed = queue.claim_next(conn)
    assert claimed["id"] == ack["id"]
    rep = report.failed_system(attempts=1, stage="gate", reason="zone refused")
    queue.finish(conn, ack["id"], status=capture_schema.FAILED, expected_attempts=claimed["attempts"],
                error=rep["summary"], report=rep)

    reported = _run(poller.poll_once(ctx))

    assert reported == 0
    assert gw.posted == []


# ── the rewrite notice: telling the person whose page changed ──────────────────────────────────
# A capture may bring an existing page up to date, and NOTHING proves the new text is right: the
# bytes are the ones the agent just wrote, so comparing them to what the agent wrote proves
# nothing. What stands in place of a proof is that the change is loud and has an owner — the diff
# is attributed, `git revert` is the undo, and the person who filed the page is told. This pass IS
# that last clause, which is why it is not a nicety and why these tests exist.
def _filed_with_a_rewrite(conn, ctx, *, owner: str, submission_id: int) -> None:
    rep = report.filed(page_path="wiki/notes/New.md", commit="cafe1234",
                       anchoring={"entities": ["Acme Corp"]}, links=[], overlaps=[], findings=[],
                       pages_rewritten=[{"path": "wiki/notes/Renewal Terms.md",
                                         "submitted_by": owner,
                                         "why": "the 30-day window was superseded in August"}])
    _claim_and_finish(conn, submission_id, status=capture_schema.FILED, report_dict=rep,
                      result_ref="wiki/notes/New.md@cafe1234")


def test_the_person_who_filed_a_rewritten_page_is_told_what_changed_and_why(indexed, clean_tables):
    """The DM goes to the page's OWN submitter — not to whoever captured — and carries the reason
    the account gave. Without this, a rewrite is a silent overwrite of somebody else's work."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.users["UANA"] = "ana@acme.com"
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="9.1")
    _filed_with_a_rewrite(conn, ctx, owner="ana@acme.com", submission_id=submission_id)

    sent = _run(poller.notify_rewrites_once(ctx))

    assert sent == 1
    dm = gw.posted[-1]
    assert dm.channel_id == "UANA"          # her, not the capture's thread
    assert "wiki/notes/Renewal Terms.md" in dm.text
    assert "superseded in August" in dm.text
    assert "git revert" in dm.text          # the undo is named, and nothing is asked of her


def test_a_rewrite_notice_is_sent_exactly_once_across_passes(indexed, clean_tables):
    """At-least-once with a record, exactly like the outcome report next door: the row is written
    after the DM, so a pass that dies between them repeats one notice rather than losing it."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.users["UANA"] = "ana@acme.com"
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="9.2")
    _filed_with_a_rewrite(conn, ctx, owner="ana@acme.com", submission_id=submission_id)

    assert _run(poller.notify_rewrites_once(ctx)) == 1
    assert _run(poller.notify_rewrites_once(ctx)) == 0
    assert len([p for p in gw.posted if p.channel_id == "UANA"]) == 1


def test_an_owner_this_workspace_does_not_have_is_recorded_rather_than_retried_forever(
        indexed, clean_tables):
    """The brain's identities are not all Slack members: a page filed by somebody who has left has
    nowhere for its notice to go. That is a fact to record, not a failure to retry — otherwise the
    pass looks them up again every five seconds for the life of the deployment."""
    conn, fixture = indexed
    gw = FakeSlackGateway()                  # nobody at that address
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="9.3")
    _filed_with_a_rewrite(conn, ctx, owner="gone@acme.com", submission_id=submission_id)

    assert _run(poller.notify_rewrites_once(ctx)) == 0
    assert not [p for p in gw.posted if p.channel_id.startswith("U")]
    assert _run(poller.notify_rewrites_once(ctx)) == 0     # ...and not asked again


def test_an_ordinary_filing_notifies_nobody(indexed, clean_tables):
    """**The benign twin.** A capture that rewrote nothing must produce no DM at all: a notice
    people learn to ignore is worse than no notice, and this pass runs on every poll."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _new_submission(ctx, identity=fixture.STEWARD, channel_id="C1", thread_ts="9.4")
    rep = report.filed(page_path="wiki/notes/New.md", commit="cafe1234",
                       anchoring={"entities": ["Acme Corp"]}, links=[], overlaps=[], findings=[])
    _claim_and_finish(conn, submission_id, status=capture_schema.FILED, report_dict=rep,
                      result_ref="wiki/notes/New.md@cafe1234")

    assert _run(poller.notify_rewrites_once(ctx)) == 0
    assert gw.posted == []
