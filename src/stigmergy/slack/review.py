"""The Slack review surface: buttons on a doorbell DM that call `review_decide`, and the one modal
a verdict needs first — a merge, which has to know which registered entity the proposal really
is. **The bot decides nothing**: it resolves who is asking and calls the SAME `review_decide_safe`
every MCP caller calls, so `review_decide`'s own validation stays the one place a decision is
made. Two item kinds reach a human here: an identity the librarian proposed and a spelling it
proposed. `items_for_doorbell` is the one read this module reuses (never re-queried a second
way), so the merge modal can offer the proposal's own merge candidates at click time.
"""
import json
import logging

from stigmergy.review_kinds import KIND_IDENTITY_PROPOSAL
from stigmergy.server import review
from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import Resolved

log = logging.getLogger(__name__)

# The one (kind, verdict) pair that opens a modal before `review_decide` can be called at all.
# Every OTHER button on this surface fires directly — friction only where a second fact is
# actually required.
_MODAL_VERDICTS = {(KIND_IDENTITY_PROPOSAL, "merge")}


def _parse_action_id(action_id: str) -> tuple[str, str, str] | None:
    """`(mode, kind, verdict_token)` from `review:<kind>:<verdict>` (fire directly) or
    `review-modal:<kind>:<verdict>` (open a modal first) — `None` for any action_id this module
    does not own, so a listener handling other actions can fall through cleanly."""
    for prefix, mode in ((render.MODAL_ACTION_PREFIX, "modal"),
                        (render.DIRECT_ACTION_PREFIX, "direct")):
        if action_id.startswith(prefix):
            rest = action_id[len(prefix):]
            kind, sep, verdict_token = rest.partition(":")
            if sep:
                return mode, kind, verdict_token
    return None


def _confirmation_text(result: dict, *, kind: str, item_id: str, verdict: str) -> str:
    if "error" in result:
        return result["error"]
    return (result.get("message")
            or copy.decision_recorded(verdict=verdict, kind=kind, item_id=item_id,
                                      actor=result.get("actor", "")))


async def _post_decision_confirmation(ctx, *, channel_id: str, result: dict, kind: str,
                                      item_id: str, verdict: str, what: str) -> None:
    """Post `review_decide`'s own confirmation (or clean refusal) back to the steward — decided
    in ONE place; every confirmation is the same plain message, whatever the verdict.

    `render.escape_and_clamp` here rather than per branch, for the same reason the doorbell cards
    escape every slot: this text is composed from `review_decisions` rows (a refusal names the
    actor who decided first, and nothing sanitizes that column at the writer) and from entity
    names lifted out of captured material, and a Slack `text` field is mrkdwn — so one branch left
    raw is a live link in a steward's DM."""
    text = _confirmation_text(result, kind=kind, item_id=item_id, verdict=verdict)
    await ctx.post_or_log(
        ctx.gateway.chat_post_message(channel_id, text=render.escape_and_clamp(text)), what=what)


async def _decide_and_confirm(ctx, service, *, channel_id: str, kind: str, item_id: str,
                              verdict: str, what: str, into: str = "") -> None:
    """Make the decision and say what happened — including when it goes wrong.
    `review_decide_safe` converts CLEAN refusals into a result dict; an UNANTICIPATED exception
    still propagates and is caught here as a GENERIC failure message. Silence instead is
    expensive: a decision PUSHES before it records itself, so an untold steward retries and is
    refused for a decision they were never told had landed."""
    try:
        # The ONE place this surface enters the review lane — button and merge modal both funnel
        # here — so `source` is stamped once and no handler can forget it.
        result = review.review_decide_safe(service, item_kind=kind, item_id=item_id,
                                           verdict=verdict, source=review.SOURCE_SLACK, into=into)
    except Exception:
        log.error("slack review: review_decide failed for %s:%s", kind, item_id, exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_message(channel_id, blocks=render.render_server_error(),
                                          text=copy.server_error()),
            what=f"review-decide server-error for {kind}:{item_id}")
        return
    await _post_decision_confirmation(ctx, channel_id=channel_id, result=result, kind=kind,
                                      item_id=item_id, verdict=verdict, what=what)


async def _open_modal(ctx, *, trigger_id: str, view: dict, what: str) -> None:
    """`views.open`, failure logged and swallowed — never raised into the listener (Slack's
    `trigger_id` is single-use and expires quickly, so a failed open cannot be retried with the
    same one anyway)."""
    try:
        await ctx.gateway.views_open(trigger_id=trigger_id, view=view)
    except SlackApiError:
        log.error("slack review: could not open %s", what, exc_info=True)


def _merge_modal_inputs(conn, item_id: str) -> tuple[str, list[dict]]:
    """The proposal's name and its merge candidates, off the SAME item the doorbell rendered.
    `items_for_doorbell` is the management-shaped, unscoped read — the candidates are registry
    names, which `list_entities` serves to every identity anyway, so nothing here widens what a
    steward may see. `("", [])` when the item can no longer be found among the OPEN items (already
    decided between the DM and this click): the modal still opens with the typed field alone, and
    `review_decide`'s own validation is what refuses a stale merge on submit."""
    for item in review.items_for_doorbell(conn):
        if item["kind"] == KIND_IDENTITY_PROPOSAL and str(item["id"]) == str(item_id):
            return str(item.get("name") or ""), list(item.get("merge_candidates") or [])
    return "", []


def _text_value(state_values: dict, block_id: str, action_id: str) -> str:
    block = (state_values or {}).get(block_id) or {}
    return ((block.get(action_id) or {}).get("value") or "").strip()


def _selected_option_value(state_values: dict, block_id: str, action_id: str) -> str:
    block = (state_values or {}).get(block_id) or {}
    selected = (block.get(action_id) or {}).get("selected_option") or {}
    return str(selected.get("value") or "")


async def handle_block_action(ctx, *, action_id: str, value: str, trigger_id: str,
                              channel_id: str, slack_user_id: str, event_team_id: str) -> None:
    """One button click on a doorbell card. `value` is always the bare item id this surface's OWN
    render functions put there — never text a caller supplied: nothing untrusted ever becomes a
    button value on this surface.

    `private_metadata` carries only WHAT the eventual decision is about (item kind, id, where to
    post the confirmation) — never WHO is making it. The click that opens the merge modal is
    itself resolved from this call's own `slack_user_id`/`event_team_id`; the modal's own
    submission re-resolves identity independently, from ITS OWN authoritative body, rather than
    trusting a value this code stamped and round-tripped through the client.
    """
    parsed = _parse_action_id(action_id)
    if parsed is None:
        return   # not one of this module's actions
    mode, kind, verdict_token = parsed
    item_id = value

    identity_result = await ctx.resolve_slack_identity(event_team_id=event_team_id,
                                                       slack_user_id=slack_user_id)
    if not isinstance(identity_result, Resolved):
        return   # silently declined — a steward whose own identity fails to resolve gets no
                 # feedback that leaks whether the button itself was valid.

    if mode == "modal":
        if (kind, verdict_token) not in _MODAL_VERDICTS:
            # A button from an OLDER deploy can carry a (kind, verdict) this build no longer maps
            # to a modal. A worded decline keeps this a normal, informative refusal instead of an
            # opaque failure in the listener's last-resort backstop.
            log.error("slack review: no modal configured for stale action %s:%s", kind,
                      verdict_token)
            await ctx.post_or_log(
                ctx.gateway.chat_post_message(channel_id, text=copy.STALE_REVIEW_ACTION),
                what=f"stale review action {kind}:{verdict_token}")
            return
        name, candidates = _merge_modal_inputs(ctx.conn, item_id)
        metadata = json.dumps({"item_kind": kind, "item_id": item_id, "channel_id": channel_id})
        view = render.render_merge_modal(private_metadata=metadata, name=name or item_id,
                                         candidates=candidates)
        await _open_modal(ctx, trigger_id=trigger_id, view=view,
                          what=f"the merge modal for {item_id}")
        return

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    await _decide_and_confirm(
        ctx, service, channel_id=channel_id, kind=kind, item_id=item_id, verdict=verdict_token,
        what=f"review-decide confirmation for {kind}:{item_id}")


async def handle_merge_modal_submission(ctx, *, private_metadata: str, state_values: dict,
                                        slack_user_id: str, event_team_id: str) -> None:
    """The merge modal's `view_submission` — `private_metadata` is Slack's own round-trip
    mechanism for WHICH proposal this survivor is FOR; no server-side store needed. The survivor
    is the selected candidate, or the typed registry id when the steward chose none.

    **`slack_user_id`/`event_team_id` name WHO is submitting, and come from the listener's own
    authoritative `body` at submission time — never from `private_metadata`.** Stamping them into
    the metadata when the modal was OPENED would make the submitter a value this code itself
    wrote, not a fact Slack is asserting about who just clicked Submit.
    """
    try:
        metadata = json.loads(private_metadata or "{}")
    except json.JSONDecodeError:
        log.error("slack review: merge modal private_metadata was not valid JSON")
        return
    kind = metadata.get("item_kind", "")
    item_id = metadata.get("item_id", "")
    channel_id = metadata.get("channel_id", "") or slack_user_id

    into = (_selected_option_value(state_values, render.MERGE_SELECT_BLOCK_ID,
                                   render.MERGE_SELECT_ACTION_ID)
            or _text_value(state_values, render.MERGE_TYPED_BLOCK_ID,
                           render.MERGE_TYPED_ACTION_ID))

    identity_result = await ctx.resolve_slack_identity(event_team_id=event_team_id,
                                                       slack_user_id=slack_user_id)
    if not isinstance(identity_result, Resolved):
        return
    if not into:
        await ctx.post_or_log(
            ctx.gateway.chat_post_message(channel_id, text=copy.MERGE_NEEDS_TARGET),
            what=f"merge without a survivor for {kind}:{item_id}")
        return

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    await _decide_and_confirm(
        ctx, service, channel_id=channel_id, kind=kind, item_id=item_id, verdict="merge",
        what=f"review-decide (merge) confirmation for {kind}:{item_id}", into=into)
