"""`@brain <question>` and the channel/DM split — against real Postgres, with the offline `fake`
answer synthesizer (keyless) and an offline Slack double.
"""
import asyncio
import dataclasses

import pytest

from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import RateLimitError
from stigmergy.server.ratelimit import RateLimiter
from stigmergy.slack import copy, mention
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.identity import ForeignTeam, Ignored, NoAccess, Resolved, TransientFailure
from tests import testdb
from tests.server.conftest import Fixture
from tests.slack.conftest import FINANCE_CHANNEL, TEAM_ID, UNLISTED_CHANNEL, build_context

pytestmark = pytest.mark.timeout(30)

ACME_Q = "what is the total compensation for acme?"
ARR_Q = "what is the arr-usd for initech in 2026-03?"

STEWARD = Resolved(email=Fixture.STEWARD, audiences=None)
ANA = Resolved(email=Fixture.ANA, audiences=frozenset({"finance"}))


def _run(coro):
    return asyncio.run(coro)


def _ask(ctx, *, channel_id, is_dm, question, identity, asker_slack_user_id="U_ASKER",
        thread_ts="1.1"):
    _run(mention.handle_mention(ctx, event_team_id=TEAM_ID, channel_id=channel_id,
                                thread_ts=thread_ts, is_dm=is_dm,
                                asker_slack_user_id=asker_slack_user_id, question=question,
                                identity_result=identity))


def _updated_text(gw: FakeSlackGateway) -> str:
    """Concatenate every mrkdwn text of the LAST edit — the channel-visible answer. Pulls from
    both `section` blocks (the answer body) and `context` blocks (the Sources block and the
    verdict line render as `context`, so Slack draws them smaller and grey)."""
    blocks = gw.updated[-1].blocks or []
    parts = []
    for b in blocks:
        if b.get("type") == "section":
            parts.append(b["text"]["text"])
        elif b.get("type") == "context":
            parts.extend(e["text"] for e in b.get("elements", []) if e.get("type") == "mrkdwn")
    return "\n".join(parts)


def test_placeholder_is_posted_first_then_edited_in_the_same_thread(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD, thread_ts="42.1")

    assert gw.posted[0].text == copy.PLACEHOLDER
    assert gw.posted[0].thread_ts == "42.1"
    assert gw.updated[0].ts == gw.posted[0].ts       # the SAME message, edited
    assert gw.updated[0].channel_id == "D1"


def test_a_verified_answer_renders_its_verdict_line_and_citation(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)
    text = _updated_text(gw)
    assert "512000" in text
    assert copy.VERDICT_LINES["verified"] in text


# ── the asker's email never travels in a public channel's block payload ────────────────────────
def test_the_askers_email_never_appears_anywhere_in_the_rendered_blocks(indexed, clean_tables):
    """The OLD "show it here" button embedded `json.dumps({"path": ..., "asker_email": ...})` —
    retrievable by any workspace member via `conversations.history` and by any other app with
    history scope. This drives a REAL channel question end to end (an unlinked citation is
    guaranteed here, since `no_link_resolver` resolves every path to `None`) and inspects the
    actual posted/updated Block Kit payload, not just `render.py` in isolation."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=ANA,
        asker_slack_user_id="U_ANA")

    import json
    everything = json.dumps([{"blocks": p.blocks} for p in gw.posted]
                            + [{"blocks": u.blocks} for u in gw.updated])
    assert fixture.ANA not in everything
    assert "asker_email" not in everything


# ── the channel scopes the thread ──────────────────────────────────────────────────────────────
def test_unlisted_channel_gives_an_empty_scope_even_to_an_unrestricted_asker(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ACME_Q, identity=STEWARD)
    text = _updated_text(gw)
    assert text.startswith("*I don't have that.*")   # acme's payroll (acl=finance) is invisible


def test_channel_never_states_or_implies_that_more_exists(indexed, clean_tables):
    """Checked directly on the one channel message: nothing hedges about content it cannot
    show."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ACME_Q, identity=STEWARD)
    text = _updated_text(gw).lower()
    for hedge in ("there may be more", "i can't show here", "elsewhere", "restricted"):
        assert hedge not in text


# ── identical channel-visible bytes for a scoped and an unrestricted asker ─────────────────────
def test_channel_answer_is_byte_identical_for_a_scoped_and_an_unrestricted_asker(indexed, clean_tables):
    conn, fixture = indexed
    gw_steward = FakeSlackGateway()
    ctx_steward = build_context(fixture, conn, gateway=gw_steward)
    _ask(ctx_steward, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=STEWARD,
        asker_slack_user_id="U_STEWARD")

    gw_ana = FakeSlackGateway()
    ctx_ana = build_context(fixture, conn, gateway=gw_ana)
    _ask(ctx_ana, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=ANA,
        asker_slack_user_id="U_ANA")

    assert _updated_text(gw_steward) == _updated_text(gw_ana)


# ── criteria 9/10: the cheap comparison and the DM fallback ──────────────────────────────────────
def test_a_wider_asker_scope_that_surfaces_more_gets_a_dm(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ACME_Q, identity=STEWARD,
        asker_slack_user_id="U_STEWARD")

    dm_posts = [p for p in gw.posted if p.channel_id == "U_STEWARD"]
    assert len(dm_posts) == 1
    dm_text = "\n".join(b["text"]["text"] for b in (dm_posts[0].blocks or [])
                        if b.get("type") == "section")
    assert "750000" in dm_text
    assert f"#{UNLISTED_CHANNEL}" not in dm_text   # channel NAME is used, not its raw id


def test_no_dm_when_the_askers_scope_cannot_be_wider_than_the_channels(indexed, clean_tables):
    """Asserted by counting `ask` invocations: Ana's own scope (`["finance"]`) is
    EXACTLY the finance channel's own scope, so `_scope_could_be_wider` short-circuits before even
    the cheap two `search()` calls run — the (expensive) second `ask` must never run, and no DM is
    sent."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=ANA,
        asker_slack_user_id="U_ANA")
    dm_posts = [p for p in gw.posted if p.channel_id == "U_ANA"]
    assert dm_posts == []


def _count_run_ask_calls(monkeypatch):
    """Assert that the MECHANISM fired: a DM post list that is empty does not by itself prove the
    expensive second `ask()` never ran (a bug could run it
    and simply drop or fail to post the result); counting calls to the one seam every `ask` goes
    through closes that gap."""
    calls = []
    real_run_ask = mention._run_ask

    async def _counting(service, question):
        calls.append(question)
        return await real_run_ask(service, question)

    monkeypatch.setattr(mention, "_run_ask", _counting)
    return calls


def test_no_dm_case_runs_ask_exactly_once_counted_directly(indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    calls = _count_run_ask_calls(monkeypatch)
    _ask(ctx, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=ANA,
        asker_slack_user_id="U_ANA")
    assert len(calls) == 1   # the channel answer only — the DM comparison never reaches ask()


def test_wider_dm_case_runs_ask_exactly_twice_counted_directly(indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    calls = _count_run_ask_calls(monkeypatch)
    _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ACME_Q, identity=STEWARD,
        asker_slack_user_id="U_STEWARD")
    assert len(calls) == 2   # the channel answer, then the DM's fuller answer — never a third


# ── the withholding comparison must not spend the asker's OWN rate-limit budget ────────────────
def test_the_dm_comparison_and_fuller_answer_do_not_spend_the_askers_own_rate_limit_budget(
        indexed, clean_tables):
    """The forbidden shape: an asker for whom content was withheld must not become measurably
    likelier to hit the PUBLIC rate-limit message on their next real question — that is itself a
    signal. The comparison/DM services used to be built through the SAME shared, per-identity
    `RateLimiter` the asker's own real calls spend, so their two extra `search()` calls, plus the
    extra `ask()`, silently ate into the asker's own budget."""
    from stigmergy.server.ratelimit import RateLimiter
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    ctx.rate_limiter = RateLimiter(overall_per_min=1, ask_per_min=1)
    # Exhaust the asker's ENTIRE shared budget with one ordinary, real call first — what is left
    # is exactly what a genuine question of theirs would need, and nothing else.
    ctx.build_service(Fixture.STEWARD, None).search("warm the bucket")

    # The comparison machinery must still run cleanly even though the identity's SHARED budget is
    # already spent — it must draw on its own unmetered allowance, never the asker's.
    _run(mention._maybe_dm_fuller_answer(
        ctx, email=Fixture.STEWARD, asker_audiences=None, asker_slack_user_id="U_STEWARD",
        channel_id=UNLISTED_CHANNEL, question=ACME_Q, effective_audiences=set()))

    dm_posts = [p for p in gw.posted if p.channel_id == "U_STEWARD"]
    assert len(dm_posts) == 1   # the fuller DM answer still shipped — no RateLimitError anywhere


# ── a DM uses the asker's own scope, unrestricted included ─────────────────────────────────────
def test_a_dm_question_uses_the_askers_own_unrestricted_scope(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D_STEWARD", is_dm=True, question=ACME_Q, identity=STEWARD)
    text = _updated_text(gw)
    assert "750000" in text and not text.startswith("*I don't have that.*")


def test_a_dm_question_uses_the_askers_own_scoped_scope_too(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D_ANA", is_dm=True, question=ACME_Q, identity=ANA)
    text = _updated_text(gw)
    assert "750000" in text   # Ana's own scope includes finance


# ── identity failures ────────────────────────────────────────────────────────────────────────
def test_no_access_in_a_channel(indexed, clean_tables):
    """A NoAccess reply in a CHANNEL must be ephemeral (visible only to the asker). It used to go
    through `chat_post_message`, disclosing one person's access status to everyone in the channel
    — an unconsented disclosure, and a public oracle over the identity registry."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ARR_Q, identity=NoAccess())
    assert gw.posted == []   # zero chat_post_message calls (A2's own assertion)
    assert len(gw.ephemeral) == 1
    assert gw.ephemeral[0].text == copy.no_access(is_dm=False)


def test_no_access_in_a_dm_uses_the_dm_copy(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=NoAccess())
    assert gw.posted[0].text == copy.no_access(is_dm=True)
    assert gw.ephemeral == []   # a DM has no "ephemeral to yourself" — it must be a real message


def test_transient_identity_failure_is_distinguished_from_no_access(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ARR_Q,
        identity=TransientFailure("boom"))
    assert gw.posted == []
    assert gw.ephemeral[0].text == copy.TRANSIENT_IDENTITY_FAILURE


# ── asserted by test double, not by reading the reply text ─────────────────────────────────────
def test_no_brain_service_is_constructed_on_any_non_resolved_identity_path(indexed, clean_tables):
    """Every non-`Resolved` outcome (`NoAccess`, `TransientFailure`, `Ignored`, `ForeignTeam`) must
    reach `handle_mention` without ever calling `ctx.build_service` — asserted by a test double that
    RAISES if invoked, not by inspecting the queue or the reply copy (which only proves the reply
    was right, not that a `BrainService` was never built to produce it)."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("BrainService must not be constructed on this identity path")

    ctx.build_service = _must_not_be_called
    for identity in (NoAccess(), TransientFailure("boom"), Ignored("bot_message"),
                     ForeignTeam("T_OTHER")):
        _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ARR_Q, identity=identity)


# ── an Ignored/ForeignTeam identity produces ZERO Slack traffic through the actual
# handler (not just at `is_ignorable_event`'s/`resolve_slack_identity`'s own unit-test level, which
# proves the CLASSIFICATION is right but not that `handle_mention` does nothing with it) ───────────
def test_ignored_and_foreign_team_identities_produce_zero_slack_traffic_through_the_handler(
        indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    for identity in (Ignored("bot_message"), ForeignTeam("T_OTHER")):
        _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ARR_Q, identity=identity)
    assert gw.posted == [] and gw.updated == [] and gw.ephemeral == []


# ── error states ──────────────────────────────────────────────────────────────────────────────
def test_a_rate_limited_identity_gets_the_rate_limit_copy(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()

    class _AlwaysOverBudget:
        def check(self, identity, tool):
            raise RateLimitError("over budget")

    ctx = build_context(fixture, conn, gateway=gw)
    ctx.rate_limiter = _AlwaysOverBudget()
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)
    assert _updated_text(gw) == copy.RATE_LIMIT


def test_an_ask_timeout_edits_the_placeholder_into_the_honest_took_too_long_message(
        indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)

    async def _timeout(*_a, **_kw):
        raise TimeoutError()

    monkeypatch.setattr(mention, "_run_ask", _timeout)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)
    assert _updated_text(gw) == copy.TIMEOUT


def test_an_unexpected_error_never_leaks_a_traceback_or_none(indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)

    async def _boom(*_a, **_kw):
        raise RuntimeError("/etc/some/internal/path leaked here")

    monkeypatch.setattr(mention, "_run_ask", _boom)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)
    text = _updated_text(gw)
    assert "/etc/" not in text
    assert "RuntimeError" not in text
    assert "None" not in text
    assert "Something went wrong on my end" in text


# ── the dead-database failure mode, driven for real
# (not a mocked exception) — the real connection `ask()` runs its query against is closed BEFORE
# the mention is handled, so whatever `psycopg` raises is what the generic-exception branch must
# turn into the honest server-error copy with no DSN, path, exception class or `None` in it.
def test_a_dead_database_never_leaks_a_dsn_class_name_or_none(indexed, clean_tables):
    conn, fixture = indexed
    dead_conn = testdb.connect_or_skip("mention-dead-db")
    dead_conn.close()   # the real failure mode: `ask()` runs its query against a closed connection
    gw = FakeSlackGateway()
    ctx = build_context(fixture, dead_conn, gateway=gw)

    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)

    text = _updated_text(gw)
    assert "Something went wrong on my end" in text
    for forbidden in ("psycopg", "OperationalError", "InterfaceError", "Traceback", "None"):
        assert forbidden not in text
    assert testdb.dsn() not in text
    assert "localhost" not in text and "54321" not in text


# ── ruling 3: retry the edit once, then fall back to a new message ───────────────────────────────
def test_edit_retries_once_then_succeeds(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.fail_update_count = 1
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)
    assert len(gw.updated) == 1     # the SECOND attempt succeeded
    assert not any(p.text != copy.PLACEHOLDER for p in gw.posted)   # no fallback post needed


def test_edit_fails_twice_then_falls_back_to_a_new_message_in_the_thread(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.fail_update_count = 2
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD, thread_ts="9.1")
    assert gw.updated == []
    fallback = [p for p in gw.posted if p.text != copy.PLACEHOLDER]
    assert len(fallback) == 1
    assert fallback[0].thread_ts == "9.1"
    assert "512000" in fallback[0].text or any(
        "512000" in b["text"]["text"] for b in (fallback[0].blocks or []) if b.get("type") == "section")


# ── issue #32: a duplicate-citation answer must not strand the placeholder ─────────────────────
# `render.render_answer` (see the render-level reproduction in `tests/slack/test_render.py`) can be
# handed a LEGAL `answer` shape where `citations` cites the same page twice —
# `stigmergy.answer.synthesize.Citation` carries no uniqueness constraint on `path`, and
# `AnswerService._shape` ships `out.citations` straight through with no dedup. Driven here through
# `mention._run_ask`, monkeypatched exactly the way
# `test_an_ask_timeout_edits_the_placeholder_into_the_honest_took_too_long_message` and
# `test_an_unexpected_error_never_leaks_a_traceback_or_none` above already control the answer for
# an error-path test — the ONLY faithful way to drive this shape here, because the offline `fake`
# answer backend `indexed` wires (`answer.synthesize.FakeSynthesizer.run`) always builds
# `citations=[Citation(...)]`, structurally at most one, so it can never reproduce this on its own;
# monkeypatching `_run_ask` is not "faking what we claim to prove" about the render/post-or-edit
# path under test — it controls the one upstream input (a real model CAN and DID produce this
# shape, per the issue) while leaving `render.render_answer`, `_edit_or_fallback` and the
# uniqueness-enforcing `FakeSlackGateway` all real.
DUPLICATE_CITATION_ANSWER = {
    "question": ACME_Q, "refused": False,
    "answer_markdown": "Acme's ARR was 750000 usd, backed by the same page twice.",
    "reason": "", "citations": [
        {"path": "wiki/notes/acme-payroll.md", "quote": "ARR was 750000 in H1"},
        {"path": "wiki/notes/acme-payroll.md", "quote": "ARR was 750000 in H2 too"},
    ],
    "confidence": "high", "verdict": {"verdict": "verified", "unverified_figures": [],
                                     "citation_problems": []},
    "retried": False, "suppressed": False, "built_at": "2026-07-28T00:00:00Z",
}

SINGLE_CITATION_ANSWER = {
    **DUPLICATE_CITATION_ANSWER,
    "answer_markdown": "Acme's ARR was 750000 usd last quarter.",
    "citations": [{"path": "wiki/notes/acme-payroll.md", "quote": "ARR was 750000"}],
}


def _last_delivery(gw: FakeSlackGateway):
    """The last non-placeholder message actually delivered to the channel, whichever leg
    delivered it — a successful edit, or a fallback post. `None` if the placeholder was left
    standing (neither succeeded): the terminal-state failure this section's tests exist to catch."""
    if gw.updated:
        return gw.updated[-1]
    fallback = [p for p in gw.posted if p.text != copy.PLACEHOLDER]
    return fallback[-1] if fallback else None


def _delivery_text(delivery) -> str:
    """Every mrkdwn/plain text a delivered record would actually show: blocks (section/context
    text) if it carries any, else its own plain `text` — the degrade leg's text-only shape has no
    blocks at all, so its content lives only in `.text`."""
    parts = []
    for b in (delivery.blocks or []):
        if b.get("type") == "section":
            parts.append(b["text"]["text"])
        elif b.get("type") == "context":
            parts.extend(e["text"] for e in b.get("elements", []) if e.get("type") == "mrkdwn")
    return "\n".join(parts) if parts else delivery.text


def test_a_duplicate_citation_answer_is_delivered_not_stranded_as_a_placeholder(
        indexed, clean_tables, monkeypatch):
    """The reproduction (issue #32, observed live on staging): the answer cites ONE page TWICE.
    Before the fix, `render.render_answer` builds two "Show it here" actions blocks sharing
    `block_id = f"show_it_here:{path}"`; against the block_id-uniqueness-enforcing
    `FakeSlackGateway` (real Slack behaviour, mirrored — see `gateway._raise_if_invalid_blocks`),
    both the edit AND the same-blocks fallback post fail identically (`invalid_blocks`), so the
    placeholder is left standing forever and the user gets nothing even though `ask()` itself
    succeeded. The one contract this test pins: the answer reaches the channel — by edit or by a
    fallback post — and the placeholder alone is never the terminal state."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)

    async def _duplicate_citation_answer(*_a, **_kw):
        return DUPLICATE_CITATION_ANSWER

    monkeypatch.setattr(mention, "_run_ask", _duplicate_citation_answer)
    _ask(ctx, channel_id="D1", is_dm=True, question=ACME_Q, identity=STEWARD)

    delivery = _last_delivery(gw)
    assert delivery is not None, (
        "the placeholder was left standing forever: neither the edit nor a fallback post "
        "delivered the answer")
    assert "750000" in _delivery_text(delivery)


def test_when_every_blocks_carrying_call_is_rejected_the_answer_still_lands_as_plain_text(
        indexed, clean_tables, monkeypatch):
    """The degrade leg (decided fix (c)). Slack's real `invalid_blocks` has causes besides a
    duplicate block_id (an unsupported block type, a nesting/length limit, ...) —
    `FakeSlackGateway.fail_any_blocks` stands in for any of them generically, rejecting EVERY
    blocks-carrying call even with unique ids: a SINGLE, undeduplicated citation here — deliberately
    NOT the duplicate-citation scenario the reproduction above and `test_render.py` already cover,
    so this test isolates "blocks are invalid for some other reason" from "blocks collide". Armed
    from INSIDE the monkeypatched `_run_ask`, after it returns the answer — arming it any earlier
    would also break the placeholder's own first post (already covered by
    `test_the_placeholders_own_first_post_failing_does_not_raise_out_of_the_handler`), which is a
    different failure mode this test does not concern itself with.

    Before the fix, `_edit_or_fallback` retries the edit with the SAME blocks and then posts the
    SAME blocks again as the fallback — every attempt carries `blocks` and fails identically, so
    NOTHING is ever delivered, not even the `text=` companion `mention._answer_fallback_text`
    already sends alongside those blocks today (a stub, "Here's an answer.", never the real answer
    body). The fix must degrade to a TEXT-ONLY send (no `blocks` at all) whose text is a REAL
    rendering of the answer, containing the answer body."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)

    async def _single_citation_answer_then_reject_all_blocks(*_a, **_kw):
        gw.fail_any_blocks = True   # armed only NOW — after the placeholder post already succeeded
        return SINGLE_CITATION_ANSWER

    monkeypatch.setattr(mention, "_run_ask", _single_citation_answer_then_reject_all_blocks)
    _ask(ctx, channel_id="D1", is_dm=True, question=ACME_Q, identity=STEWARD)

    delivery = _last_delivery(gw)
    assert delivery is not None, (
        "the placeholder was left standing forever: the degrade leg must still deliver something "
        "even when EVERY blocks-carrying call fails")
    assert not delivery.blocks, f"the degrade leg must carry no blocks, got {delivery.blocks!r}"
    assert "750000" in delivery.text
    assert delivery.text != "Here's an answer.", (
        "that is the stub fallback text — the degrade leg must ship a REAL rendering of the "
        "answer body, not the placeholder-grade stub")


# ── benign twin: an ordinary single citation must still work through the SAME
# uniqueness-enforcing fake ──────────────────────────────────────────────────────────────────────
def test_a_single_citation_answer_still_renders_and_posts_one_button_through_the_real_ask_path(
        indexed, clean_tables):
    """The benign twin for the reproduction above: an ORDINARY answer citing exactly one page once
    — the common case, driven through the REAL `fake` answer synthesizer (no monkeypatch), so this
    proves `FakeSlackGateway`'s new block_id-uniqueness enforcement does not over-reject legitimate
    traffic, only the genuinely colliding case. Must be green BEFORE and AFTER the production fix —
    it is not part of the reproduction, it is the guard against a fix that over-corrects."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)

    assert len(gw.updated) == 1   # the edit succeeded on the first attempt — no fallback needed
    blocks = gw.updated[0].blocks or []
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 1
    assert len(actions[0]["elements"]) == 1
    assert actions[0]["elements"][0]["text"]["text"] == copy.SHOW_IT_HERE_LABEL


# ── the guards around the placeholder and the channel-scope lookup ─────────────────────────────
def test_the_placeholders_own_first_post_failing_does_not_raise_out_of_the_handler(
        indexed, clean_tables):
    """The placeholder's own FIRST `chat_post_message` used to be unguarded, so a Slack outage
    there raised straight out of the listener instead of degrading honestly."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.fail_post_count = 1
    ctx = build_context(fixture, conn, gateway=gw)
    _ask(ctx, channel_id="D1", is_dm=True, question=ARR_Q, identity=STEWARD)
    assert gw.posted == [] and gw.updated == []   # no exception raised; nothing else was posted


def test_a_malformed_channels_file_gets_the_server_error_copy_not_total_silence(
        indexed, clean_tables, tmp_path):
    """`channels.channel_audiences` raising `IdentityError` used to propagate BEFORE any reply
    was posted — failing closed is right; total silence (a Bolt-logged trace nobody sees) is
    not."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    bad_channels_path = tmp_path / "bad-slack-channels.json"
    bad_channels_path.write_text("not json at all")
    ctx.settings = dataclasses.replace(ctx.settings, channels_path=str(bad_channels_path))

    _ask(ctx, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=STEWARD)

    assert len(gw.posted) == 1
    assert "Something went wrong on my end" in gw.posted[0].text
    assert gw.updated == []   # never reached the placeholder-edit path — refused before it posted


# ── the comparison search() calls are guarded the same way the DM ask() already is ─────────────
def test_a_comparison_search_failure_does_not_escape_after_the_channel_answer_already_shipped(
        indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)

    from stigmergy.server.service import BrainService
    from stigmergy.slack.mention import COMPARISON_MAX_RESULTS
    real_search = BrainService.search
    calls = {"n": 0}

    def _boom(self, *a, **kw):
        # Only the COMPARISON calls (`max_results=COMPARISON_MAX_RESULTS`) fail — the primary
        # channel answer's OWN internal search calls (a different `max_results`, via
        # `AnswerBrain.search_text`) must be unaffected, so the assertion below actually proves
        # "the channel answer already shipped" rather than "nothing ran at all".
        if kw.get("max_results") == COMPARISON_MAX_RESULTS or (
                len(a) >= 2 and a[1] == COMPARISON_MAX_RESULTS):
            calls["n"] += 1
            raise RuntimeError("search backend hiccup")
        return real_search(self, *a, **kw)

    monkeypatch.setattr(BrainService, "search", _boom)
    _ask(ctx, channel_id=UNLISTED_CHANNEL, is_dm=False, question=ACME_Q, identity=STEWARD,
        asker_slack_user_id="U_STEWARD")

    assert calls["n"] >= 1   # the comparison really ran and really failed
    assert gw.updated, "the channel answer must still have shipped"
    dm_posts = [p for p in gw.posted if p.channel_id == "U_STEWARD"]
    assert dm_posts == []   # no fuller answer — the comparison never got to decide one was needed
    monkeypatch.setattr(BrainService, "search", real_search)


# ── an IDENTITY-level scope (not a channel-level one) changes the ask result ───────────────────
def test_a_dm_question_is_refused_for_a_scoped_identity_lacking_the_needed_label(
        indexed, clean_tables):
    """No existing test shows an identity's OWN audience scope — as opposed to a CHANNEL's —
    changing an `ask` result on the Slack surface. Both asks below happen in a DM (so the channel
    scope is out of the picture entirely: `handle_mention` uses `asker_audiences` directly for a
    DM); ENG (scoped to `["eng"]`, no `"finance"`) is refused the SAME question ANA (scoped to
    `["finance"]`) is answered — the identity's own label set is what decides it."""
    conn, fixture = indexed
    ENG = Resolved(email=fixture.ENG, audiences=frozenset({"eng"}))

    gw_eng = FakeSlackGateway()
    ctx_eng = build_context(fixture, conn, gateway=gw_eng)
    _ask(ctx_eng, channel_id="D_ENG", is_dm=True, question=ACME_Q, identity=ENG)
    text_eng = _updated_text(gw_eng)
    assert text_eng.startswith("*I don't have that.*")   # ACME's payroll is acl=finance

    gw_ana = FakeSlackGateway()
    ctx_ana = build_context(fixture, conn, gateway=gw_ana)
    _ask(ctx_ana, channel_id="D_ANA", is_dm=True, question=ACME_Q, identity=ANA)
    text_ana = _updated_text(gw_ana)
    assert "750000" in text_ana
    assert not text_ana.startswith("*I don't have that.*")


# ── Slack's `ask` must go through the SAME audited, rate-limited seam every other transport uses
# — `_run_ask` used to call `AnswerService(service).ask(question)` directly, never touching
# `service.call_async`, so a Slack question left NO `audit_log` row at all and never spent the
# `ask` rate-limit bucket. `stigmergy-pilot-report` builds its metrics over `audit_log`'s `ask`
# rows, and most real asking happens in Slack — this is what those rows depend on existing in the
# first place. ────────────────────────────────────────────────────────────────────────────────
def _audit_rows_for(conn, identity: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT identity, tool, outcome, error_class FROM audit_log WHERE identity = %s"
            " ORDER BY id", (identity,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


@pytest.fixture()
def clean_audit_log(indexed):
    """`ensure_audit_table` is idempotent DDL (never drops) — mirrors
    `tests/server/test_audit.py::clean_audit_log`, scoped to this module's own `indexed` conn."""
    conn, _ = indexed
    ensure_audit_table(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM audit_log")
    return conn


def test_a_channel_ask_writes_an_audit_log_row_attributed_to_the_asker(
        indexed, clean_tables, clean_audit_log):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    ctx.audit = AuditWriter(conn)

    _ask(ctx, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=ANA,
        asker_slack_user_id="U_ANA")

    rows = _audit_rows_for(conn, Fixture.ANA)
    ask_rows = [r for r in rows if r["tool"] == "ask"]
    assert len(ask_rows) == 1
    assert ask_rows[0]["outcome"] == "ok"


def test_dm_fallback_ask_is_audited_and_still_does_not_spend_the_askers_rate_limit_budget(
        indexed, clean_tables, clean_audit_log):
    """Both properties asserted TOGETHER, so a future edit that satisfies one cannot silently
    break the other: the DM fallback's fuller `ask()` (`_maybe_dm_fuller_answer`) must write its
    OWN `audit_log` row while still never touching the asker's own shared rate-limit budget. Same
    setup as the budget test above (`overall_per_min=1`, the budget exhausted by one ordinary real
    call first) — extended here to also check the audit row that call must leave."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    ctx.audit = AuditWriter(conn)
    ctx.rate_limiter = RateLimiter(overall_per_min=1, ask_per_min=1)
    # Exhaust the asker's ENTIRE shared budget with one ordinary, real call first.
    ctx.build_service(Fixture.STEWARD, None).search("warm the bucket")

    _run(mention._maybe_dm_fuller_answer(
        ctx, email=Fixture.STEWARD, asker_audiences=None, asker_slack_user_id="U_STEWARD",
        channel_id=UNLISTED_CHANNEL, question=ACME_Q, effective_audiences=set()))

    # the budget property: the fuller DM answer still shipped — the exhausted shared budget never
    # blocked it.
    dm_posts = [p for p in gw.posted if p.channel_id == "U_STEWARD"]
    assert len(dm_posts) == 1

    # the audit property: that same `ask()` call left its own `audit_log` row.
    rows = _audit_rows_for(conn, Fixture.STEWARD)
    ask_rows = [r for r in rows if r["tool"] == "ask"]
    assert len(ask_rows) == 1
    assert ask_rows[0]["outcome"] == "ok"


def test_a_channel_ask_spends_the_ask_bucket_so_a_second_one_is_rate_limited(
        indexed, clean_tables, clean_audit_log):
    """Before this fix, exhausting the `ask`-SPECIFIC bucket (as opposed to the shared `overall`
    one) had no effect on a Slack question at all: `_run_ask` never reached `call_async`, so
    `RateLimiter.check(identity, "ask")` was never called with `tool="ask"` and the `ask` bucket
    was never consulted, let alone spent. `ask_per_min=1` isolates that bucket specifically (a
    generous `overall_per_min` means the shared bucket is never what trips)."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    ctx.audit = AuditWriter(conn)
    ctx.rate_limiter = RateLimiter(overall_per_min=1000, ask_per_min=1)

    _ask(ctx, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=ANA,
        asker_slack_user_id="U_ANA", thread_ts="1.1")
    assert "750000" in _updated_text(gw)   # the first question is answered for real

    _ask(ctx, channel_id=FINANCE_CHANNEL, is_dm=False, question=ACME_Q, identity=ANA,
        asker_slack_user_id="U_ANA", thread_ts="2.1")
    assert _updated_text(gw) == copy.RATE_LIMIT   # the SECOND spends past the ask-specific bucket

    rows = _audit_rows_for(conn, Fixture.ANA)
    ask_outcomes = sorted(r["outcome"] for r in rows if r["tool"] == "ask")
    assert ask_outcomes == ["error", "ok"]   # even the rate-limited attempt left its own audit row
